from __future__ import annotations

import atexit
import errno
import fcntl
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .executor import (
    _minimal_child_env,
    _terminate_process_group,
    _validated_argv,
    _validated_cwd,
    _workspace_root,
)
from .timing import TimingContext, current_call_context, emit_process_end

MAX_ACTIVE_SESSIONS = 3
IDLE_TTL_SECONDS = 600.0
MAX_RETAINED_OUTPUT_BYTES = 64 * 1024
MAX_POLL_OUTPUT_BYTES = 16 * 1024
MAX_WAIT_MS = 1000
_READ_CHUNK_BYTES = 8192
_READER_DRAIN_SECONDS = 0.2
_REAPER_INTERVAL_SECONDS = 1.0


@dataclass
class _Session:
    session_id: str
    process: subprocess.Popen[bytes]
    master_fd: int
    cwd: str
    argv: list[str]
    last_activity: float
    base_cursor: int = 0
    output: bytearray = field(default_factory=bytearray)
    status: str = "running"
    exit_code: int | None = None
    finalized: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    changed: threading.Condition = field(init=False)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)
    reader_done: threading.Event = field(default_factory=threading.Event)
    timing_context: TimingContext | None = None
    process_started_wall: float = 0.0
    process_started_mono: float = 0.0

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)


class TerminalSessionManager:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_ttl_seconds: float = IDLE_TTL_SECONDS,
        reaper_interval: float = _REAPER_INTERVAL_SECONDS,
        start_reaper: bool = True,
    ) -> None:
        self._clock = clock
        self._idle_ttl_seconds = float(idle_ttl_seconds)
        self._reaper_interval = float(reaper_interval)
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._stop_reaper = threading.Event()
        self._reaper_thread: threading.Thread | None = None
        if start_reaper:
            self._reaper_thread = threading.Thread(
                target=self._reaper_loop,
                name="agent-runtime-session-reaper",
                daemon=True,
            )
            self._reaper_thread.start()

    def start(self, argv: list[str], cwd: str) -> dict[str, Any]:
        checked_argv = _validated_argv(argv)
        checked_cwd = _validated_cwd(cwd, _workspace_root())

        with self._lock:
            active = sum(session.status == "running" for session in self._sessions.values())
            if active >= MAX_ACTIVE_SESSIONS:
                raise RuntimeError("maximum 3 active terminal sessions reached")

            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    checked_argv,
                    cwd=str(checked_cwd),
                    env=_minimal_child_env(),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
            except BaseException:
                os.close(master_fd)
                os.close(slave_fd)
                raise
            os.close(slave_fd)

            session = _Session(
                session_id=secrets.token_hex(8),
                process=process,
                master_fd=master_fd,
                cwd=str(checked_cwd),
                argv=checked_argv,
                last_activity=self._clock(),
                timing_context=current_call_context(),
                process_started_wall=time.time(),
                process_started_mono=time.monotonic(),
            )
            self._sessions[session.session_id] = session

        threading.Thread(
            target=self._reader,
            args=(session,),
            name=f"terminal-reader-{session.session_id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._monitor,
            args=(session,),
            name=f"terminal-monitor-{session.session_id}",
            daemon=True,
        ).start()
        return self.poll(session.session_id, cursor=0, wait_ms=0)

    def poll(self, session_id: str, cursor: int = 0, wait_ms: int = 0) -> dict[str, Any]:
        session = self._get_session(session_id)
        checked_cursor = self._validated_cursor(cursor)
        checked_wait_ms = self._validated_wait_ms(wait_ms)

        with session.changed:
            session.last_activity = self._clock()
            if (
                checked_wait_ms
                and session.status == "running"
                and checked_cursor == session.base_cursor + len(session.output)
            ):
                session.changed.wait(checked_wait_ms / 1000.0)
                session.last_activity = self._clock()

            retained_end = session.base_cursor + len(session.output)
            if checked_cursor > retained_end:
                raise ValueError("cursor is ahead of available session output")

            cursor_expired = checked_cursor < session.base_cursor
            dropped = max(0, session.base_cursor - checked_cursor)
            start_cursor = max(checked_cursor, session.base_cursor)
            start_index = start_cursor - session.base_cursor
            raw = bytes(
                session.output[
                    start_index : start_index + MAX_POLL_OUTPUT_BYTES
                ]
            )
            next_cursor = start_cursor + len(raw)
            result: dict[str, Any] = {
                "session_id": session.session_id,
                "status": session.status,
                "output": raw.decode("utf-8", errors="replace"),
                "next_cursor": next_cursor,
                "cursor_expired": cursor_expired,
                "dropped_output_bytes": dropped,
            }
            if session.status != "running":
                result["exit_code"] = session.exit_code
            return result

    def control(
        self,
        session_id: str,
        action: str,
        data: str | None = None,
        rows: int | None = None,
        cols: int | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        if action == "write":
            self._require_no_dimensions(rows, cols)
            if not isinstance(data, str):
                raise ValueError("write action requires UTF-8 string data")
            self._require_running(session)
            payload = data.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(session.master_fd, view)
                view = view[written:]
            self._touch(session)
            return self._control_result(session)

        if action == "interrupt":
            self._require_no_arguments(data, rows, cols)
            self._require_running(session)
            try:
                os.killpg(session.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            self._touch(session)
            return self._control_result(session)

        if action == "terminate":
            self._require_no_arguments(data, rows, cols)
            self._cleanup_process(session, "explicit_terminate")
            result = self._control_result(session)
            with self._lock:
                self._sessions.pop(session.session_id, None)
            return result

        if action == "resize":
            if data is not None:
                raise ValueError("resize action does not accept data")
            if (
                isinstance(rows, bool)
                or isinstance(cols, bool)
                or not isinstance(rows, int)
                or not isinstance(cols, int)
                or rows <= 0
                or cols <= 0
                or rows > 65535
                or cols > 65535
            ):
                raise ValueError("resize action requires positive integer rows and cols")
            self._require_running(session)
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(session.master_fd, termios.TIOCSWINSZ, winsize)
            self._touch(session)
            return self._control_result(session)

        raise ValueError("action must be one of: write, interrupt, terminate, resize")

    def reap_idle_once(self) -> list[str]:
        now = self._clock()
        with self._lock:
            candidates = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.last_activity >= self._idle_ttl_seconds
            ]
        expired: list[str] = []
        for session_id in candidates:
            with self._lock:
                session = self._sessions.get(session_id)
            if session is None:
                continue
            self._cleanup_process(session, "idle_reap")
            with self._lock:
                if self._sessions.pop(session_id, None) is not None:
                    expired.append(session_id)
        return expired

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def shutdown(self) -> None:
        self._stop_reaper.set()
        thread = self._reaper_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, self._reaper_interval * 2))
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            self._cleanup_process(session, "shutdown")
        with self._lock:
            self._sessions.clear()

    def _get_session(self, session_id: str) -> _Session:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("unknown or expired session_id")
        return session

    def _reader(self, session: _Session) -> None:
        try:
            while True:
                try:
                    chunk = os.read(session.master_fd, _READ_CHUNK_BYTES)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        return
                    raise
                if not chunk:
                    return
                with session.changed:
                    session.output.extend(chunk)
                    overflow = len(session.output) - MAX_RETAINED_OUTPUT_BYTES
                    if overflow > 0:
                        del session.output[:overflow]
                        session.base_cursor += overflow
                    session.changed.notify_all()
        finally:
            session.reader_done.set()
            with session.changed:
                session.changed.notify_all()

    def _monitor(self, session: _Session) -> None:
        session.process.wait()
        self._cleanup_process(session, "natural_exit")

    def _cleanup_process(self, session: _Session, termination_state: str = "natural_exit") -> None:
        with session.cleanup_lock:
            if session.finalized:
                return
            _terminate_process_group(session.process)
            session.reader_done.wait(_READER_DRAIN_SECONDS)
            with session.changed:
                if not session.finalized:
                    session.exit_code = session.process.returncode
                    session.status = "exited"
                    try:
                        os.close(session.master_fd)
                    except OSError:
                        pass
                    session.finalized = True
                    session.changed.notify_all()
            emit_process_end(
                session.timing_context,
                tool_name="terminal_start",
                process_kind="persistent_pty",
                started_wall=session.process_started_wall,
                started_mono=session.process_started_mono,
                termination_state=termination_state,
            )

    def _touch(self, session: _Session) -> None:
        with session.changed:
            session.last_activity = self._clock()

    @staticmethod
    def _validated_cursor(cursor: int) -> int:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        return cursor

    @staticmethod
    def _validated_wait_ms(wait_ms: int) -> int:
        if (
            isinstance(wait_ms, bool)
            or not isinstance(wait_ms, int)
            or wait_ms < 0
            or wait_ms > MAX_WAIT_MS
        ):
            raise ValueError(f"wait_ms must be an integer from 0 to {MAX_WAIT_MS}")
        return wait_ms

    @staticmethod
    def _require_no_dimensions(rows: int | None, cols: int | None) -> None:
        if rows is not None or cols is not None:
            raise ValueError("write action does not accept rows or cols")

    @staticmethod
    def _require_no_arguments(
        data: str | None,
        rows: int | None,
        cols: int | None,
    ) -> None:
        if data is not None or rows is not None or cols is not None:
            raise ValueError("action does not accept data, rows, or cols")

    @staticmethod
    def _require_running(session: _Session) -> None:
        with session.lock:
            if session.status != "running" or session.process.poll() is not None:
                raise RuntimeError("terminal session is not running")

    @staticmethod
    def _control_result(session: _Session) -> dict[str, Any]:
        with session.lock:
            result: dict[str, Any] = {
                "session_id": session.session_id,
                "status": session.status,
            }
            if session.status != "running":
                result["exit_code"] = session.exit_code
            return result

    def _reaper_loop(self) -> None:
        while not self._stop_reaper.wait(self._reaper_interval):
            self.reap_idle_once()


_MANAGER = TerminalSessionManager()
atexit.register(_MANAGER.shutdown)


def start_terminal(argv: list[str], cwd: str) -> dict[str, Any]:
    return _MANAGER.start(argv, cwd)


def poll_terminal(session_id: str, cursor: int = 0, wait_ms: int = 0) -> dict[str, Any]:
    return _MANAGER.poll(session_id, cursor, wait_ms)


def control_terminal(
    session_id: str,
    action: str,
    data: str | None = None,
    rows: int | None = None,
    cols: int | None = None,
) -> dict[str, Any]:
    return _MANAGER.control(session_id, action, data, rows, cols)


def shutdown_terminal_sessions() -> None:
    _MANAGER.shutdown()


def _get_session(session_id: str) -> _Session:
    return _MANAGER._get_session(session_id)
