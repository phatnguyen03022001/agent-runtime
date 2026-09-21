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

from .capacity import HeavyExecutionAdmission, HeavyExecutionLease, heavy_execution_admission
from .contracts import ARGV_ITEM_MAX_BYTES, ARGV_MAX_ITEMS, ARGV_TOTAL_MAX_BYTES, TERMINAL_DATA_MAX_BYTES
from .errors import RuntimeStateError, RuntimeValidationError
from .executor import (
    _minimal_child_env,
    _terminate_process_group,
    _validated_argv,
    _validated_cwd,
    _workspace_root,
)
from .protection import _PROTECTED_GUARD
from .timing import TimingContext, current_call_context, emit_process_end
from .tool_contract import (
    Authority,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

SESSION_LIMIT_ENV = "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS"
MAX_ACTIVE_SESSIONS = 6
DEFAULT_SESSION_LIMIT = MAX_ACTIVE_SESSIONS
IDLE_TTL_SECONDS = 600.0
MAX_RETAINED_OUTPUT_BYTES = 64 * 1024
MAX_POLL_OUTPUT_BYTES = 16 * 1024
MAX_RETAINED_COMPLETED_SESSIONS = 16
MAX_WAIT_MS = 1000
_READ_CHUNK_BYTES = 8192
_READER_DRAIN_SECONDS = 0.2
_REAPER_INTERVAL_SECONDS = 1.0

TERMINAL_START_CONTRACT = ToolContract(
    name="terminal_start",
    tool_class=ToolClass.PROCESS,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, False, True),
    preconditions={"cwd": "validated-workspace-descendant", "argv": "literal-nonempty-shell-false-protected-runtime-filtered"},
    bounds={
        "argv_items": ARGV_MAX_ITEMS,
        "argv_item_utf8_bytes": ARGV_ITEM_MAX_BYTES,
        "argv_total_utf8_bytes": ARGV_TOTAL_MAX_BYTES,
        "active_sessions": MAX_ACTIVE_SESSIONS,
        "retained_output_bytes": MAX_RETAINED_OUTPUT_BYTES,
        "idle_ttl_milliseconds": int(IDLE_TTL_SECONDS * 1000),
    },
    postconditions={"pty_process_group": True, "lifecycle": "managed-by-session-tools"},
)
TERMINAL_POLL_CONTRACT = ToolContract(
    name="terminal_poll",
    tool_class=ToolClass.PROCESS,
    authority=Authority(False, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
    annotations=ToolAnnotations(False, False, False, False),
    preconditions={"session_id": "known-or-retained-session", "cursor": "non-negative"},
    bounds={"poll_output_bytes": MAX_POLL_OUTPUT_BYTES, "wait_milliseconds": MAX_WAIT_MS},
    postconditions={"output": "bounded-incremental", "process_control": False},
)
TERMINAL_CONTROL_CONTRACT = ToolContract(
    name="terminal_control",
    tool_class=ToolClass.PROCESS,
    authority=Authority(False, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, False, True),
    preconditions={"session_id": "known-running-session", "actions": ["write", "interrupt", "terminate"]},
    bounds={"write_utf8_bytes": TERMINAL_DATA_MAX_BYTES},
    postconditions={"control": "one-explicit-session-action"},
)
TERMINAL_RESIZE_CONTRACT = ToolContract(
    name="terminal_resize",
    tool_class=ToolClass.PROCESS,
    authority=Authority(False, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
    annotations=ToolAnnotations(False, False, True, False),
    preconditions={"session_id": "known-running-session", "dimensions": "positive-integers"},
    bounds={"rows": 65535, "cols": 65535},
    postconditions={"effect": "pty-window-size-only"},
)


def effective_session_limit(raw_value: str | None = None) -> int:
    """Return the bounded operator-configured persistent PTY limit."""

    candidate = os.environ.get(SESSION_LIMIT_ENV, "") if raw_value is None else raw_value
    try:
        value = int(candidate.strip())
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_SESSION_LIMIT
    return value if 1 <= value <= MAX_ACTIVE_SESSIONS else DEFAULT_SESSION_LIMIT


def _validated_session_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ACTIVE_SESSIONS:
        raise RuntimeValidationError(
            f"max_active_sessions must be an integer from 1 through {MAX_ACTIVE_SESSIONS}"
        )
    return value


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
    cleanup_lock: threading.RLock = field(default_factory=threading.RLock)
    reader_done: threading.Event = field(default_factory=threading.Event)
    timing_context: TimingContext | None = None
    process_started_wall: float = 0.0
    process_started_mono: float = 0.0
    protected_input_buffer: str = ""
    heavy_lease: HeavyExecutionLease | None = None

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)


class TerminalSessionManager:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_ttl_seconds: float = IDLE_TTL_SECONDS,
        reaper_interval: float = _REAPER_INTERVAL_SECONDS,
        max_active_sessions: int | None = None,
        admission: HeavyExecutionAdmission | None = None,
        start_reaper: bool = True,
    ) -> None:
        self._clock = clock
        self._idle_ttl_seconds = float(idle_ttl_seconds)
        self._reaper_interval = float(reaper_interval)
        self.max_active_sessions = (
            effective_session_limit()
            if max_active_sessions is None
            else _validated_session_limit(max_active_sessions)
        )
        self._admission = heavy_execution_admission() if admission is None else admission
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
        _PROTECTED_GUARD.check(checked_argv, tool_name="terminal_start")
        checked_cwd = _validated_cwd(cwd, _workspace_root())

        with self._lock:
            active = sum(session.status == "running" for session in self._sessions.values())
            if active >= self.max_active_sessions:
                raise RuntimeStateError(
                    f"configured maximum {self.max_active_sessions} active terminal sessions reached"
                )

            lease = self._admission.acquire()
            master_fd = slave_fd = -1
            try:
                master_fd, slave_fd = pty.openpty()
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
                if master_fd >= 0:
                    os.close(master_fd)
                if slave_fd >= 0:
                    os.close(slave_fd)
                lease.release()
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
                heavy_lease=lease,
            )
            self._sessions[session.session_id] = session

        try:
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
        except BaseException:
            self._cleanup_process(session, "startup_failure")
            with self._lock:
                self._sessions.pop(session.session_id, None)
            raise
        return self.poll(session.session_id, cursor=0, wait_ms=0)

    def poll(self, session_id: str, cursor: int = 0, wait_ms: int = 0) -> dict[str, Any]:
        session = self._get_session(session_id)
        checked_cursor = self._validated_cursor(cursor)
        checked_wait_ms = self._validated_wait_ms(wait_ms)

        self._touch(session)
        waited = False
        with session.changed:
            if (
                checked_wait_ms
                and session.status == "running"
                and checked_cursor == session.base_cursor + len(session.output)
            ):
                session.changed.wait(checked_wait_ms / 1000.0)
                waited = True

        if waited:
            self._touch(session)

        with session.changed:
            retained_end = session.base_cursor + len(session.output)
            if checked_cursor > retained_end:
                raise RuntimeValidationError("cursor is ahead of available session output")

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
                raise RuntimeValidationError("write action requires UTF-8 string data")
            with session.cleanup_lock:
                buffered = (
                    session.protected_input_buffer
                    if isinstance(session.protected_input_buffer, str)
                    else ""
                )
                pending = buffered + data
                _PROTECTED_GUARD.check(["/bin/sh", "-c", pending], tool_name="terminal_control")
                self._require_running(session)
                payload = data.encode("utf-8")
                view = memoryview(payload)
                while view:
                    written = os.write(session.master_fd, view)
                    view = view[written:]
                session.protected_input_buffer = self._pending_input_suffix(pending)
                self._touch(session)
                return self._control_result(session)

        if action == "interrupt":
            self._require_no_arguments(data, rows, cols)
            with session.cleanup_lock:
                self._require_running(session)
                try:
                    os.killpg(session.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                self._touch(session)
                return self._control_result(session)

        if action == "terminate":
            self._require_no_arguments(data, rows, cols)
            with session.cleanup_lock:
                self._cleanup_process(session, "explicit_terminate")
                result = self._control_result(session)
                with self._lock:
                    self._sessions.pop(session.session_id, None)
                return result

        if action == "resize":
            if data is not None:
                raise RuntimeValidationError("resize action does not accept data")
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
                raise RuntimeValidationError("resize action requires positive integer rows and cols")
            with session.cleanup_lock:
                self._require_running(session)
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(session.master_fd, termios.TIOCSWINSZ, winsize)
                self._touch(session)
                return self._control_result(session)

        raise RuntimeValidationError("action must be one of: write, interrupt, terminate, resize")

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
            if not self._cleanup_process(session, "idle_reap"):
                continue
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
            raise RuntimeValidationError("session_id must be a non-empty string")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeValidationError("unknown or expired session_id")
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

    def _cleanup_process(self, session: _Session, termination_state: str = "natural_exit") -> bool:
        # Lock order: per-session cleanup_lock -> session.lock -> manager._lock.
        # Never acquire cleanup_lock while holding session.lock or manager._lock.
        with session.cleanup_lock:
            if session.finalized:
                return False
            if (
                termination_state == "idle_reap"
                and self._clock() - session.last_activity < self._idle_ttl_seconds
            ):
                return False
            try:
                _terminate_process_group(session.process)
                session.reader_done.wait(_READER_DRAIN_SECONDS)
                with session.changed:
                    session.exit_code = session.process.returncode
                    session.status = "exited"
                    try:
                        os.close(session.master_fd)
                    except OSError:
                        pass
                    session.finalized = True
                    self._retain_completed_session(session)
                    session.changed.notify_all()
                emit_process_end(
                    session.timing_context,
                    tool_name="terminal_start",
                    process_kind="persistent_pty",
                    started_wall=session.process_started_wall,
                    started_mono=session.process_started_mono,
                    termination_state=termination_state,
                )
                return True
            finally:
                if session.finalized and session.heavy_lease is not None:
                    session.heavy_lease.release()

    def _retain_completed_session(self, session: _Session) -> None:
        with self._lock:
            if self._sessions.get(session.session_id) is not session:
                return
            self._sessions.pop(session.session_id)
            self._sessions[session.session_id] = session
            completed_ids = [
                session_id
                for session_id, retained in self._sessions.items()
                if retained.status == "exited"
            ]
            excess = len(completed_ids) - MAX_RETAINED_COMPLETED_SESSIONS
            for session_id in completed_ids[:max(0, excess)]:
                self._sessions.pop(session_id, None)

    def _touch(self, session: _Session) -> None:
        with session.cleanup_lock:
            session.last_activity = self._clock()

    @staticmethod
    def _pending_input_suffix(text: str) -> str:
        last_newline = max(text.rfind("\n"), text.rfind("\r"))
        if last_newline >= 0:
            return text[last_newline + 1 :]
        return text

    @staticmethod
    def _validated_cursor(cursor: int) -> int:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise RuntimeValidationError("cursor must be a non-negative integer")
        return cursor

    @staticmethod
    def _validated_wait_ms(wait_ms: int) -> int:
        if (
            isinstance(wait_ms, bool)
            or not isinstance(wait_ms, int)
            or wait_ms < 0
            or wait_ms > MAX_WAIT_MS
        ):
            raise RuntimeValidationError(f"wait_ms must be an integer from 0 to {MAX_WAIT_MS}")
        return wait_ms

    @staticmethod
    def _require_no_dimensions(rows: int | None, cols: int | None) -> None:
        if rows is not None or cols is not None:
            raise RuntimeValidationError("write action does not accept rows or cols")

    @staticmethod
    def _require_no_arguments(
        data: str | None,
        rows: int | None,
        cols: int | None,
    ) -> None:
        if data is not None or rows is not None or cols is not None:
            raise RuntimeValidationError("action does not accept data, rows, or cols")

    @staticmethod
    def _require_running(session: _Session) -> None:
        with session.lock:
            if session.status != "running" or session.process.poll() is not None:
                raise RuntimeStateError("terminal session is not running")

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


def configured_session_limit() -> int:
    """Expose the effective limit for operator diagnostics and tests."""

    return _MANAGER.max_active_sessions


def _get_session(session_id: str) -> _Session:
    return _MANAGER._get_session(session_id)
