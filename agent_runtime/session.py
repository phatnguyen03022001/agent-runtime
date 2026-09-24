from __future__ import annotations

import atexit
import errno
import fcntl
import os
import pty
import re
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
RUNNING_HARD_WALL_SECONDS = 3600.0
COMPLETED_RETENTION_SECONDS = 3600.0
MAX_RETAINED_OUTPUT_BYTES = 64 * 1024
MAX_POLL_OUTPUT_BYTES = 16 * 1024
MAX_RETAINED_COMPLETED_SESSIONS = 16
MAX_WAIT_MS = 30000
_READ_CHUNK_BYTES = 8192
_READER_DRAIN_SECONDS = 0.2
_REAPER_INTERVAL_SECONDS = 1.0

TERMINAL_START_CONTRACT = ToolContract(
    name="terminal_start",
    tool_class=ToolClass.PROCESS,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, False, True),
    preconditions={
        "cwd": "validated-workspace-descendant",
        "argv": "literal-nonempty-shell-false-protected-runtime-filtered",
        "start_identity": "optional-exactly-32-lowercase-hex",
    },
    bounds={
        "argv_items": ARGV_MAX_ITEMS,
        "argv_item_utf8_bytes": ARGV_ITEM_MAX_BYTES,
        "argv_total_utf8_bytes": ARGV_TOTAL_MAX_BYTES,
        "active_sessions": MAX_ACTIVE_SESSIONS,
        "retained_output_bytes": MAX_RETAINED_OUTPUT_BYTES,
        "running_hard_wall_milliseconds": int(RUNNING_HARD_WALL_SECONDS * 1000),
        "completed_retention_milliseconds": int(COMPLETED_RETENTION_SECONDS * 1000),
        "retained_completed_sessions": MAX_RETAINED_COMPLETED_SESSIONS,
    },
    postconditions={
        "pty_process_group": True,
        "lifecycle": "managed-by-session-tools",
        "keyed_start": "reserve-before-popen-idempotent-within-runtime-incarnation",
    },
)
TERMINAL_POLL_CONTRACT = ToolContract(
    name="terminal_poll",
    tool_class=ToolClass.PROCESS,
    authority=Authority(False, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
    annotations=ToolAnnotations(False, False, False, False),
    preconditions={
        "selector": "exactly-one-of-session_id-or-start_identity",
        "session_id": "known-or-retained-session",
        "start_identity": "known-or-retained-keyed-operation",
        "cursor": "non-negative",
        "wait_for": "output_or_state-or-terminal_or_deadline",
    },
    bounds={"poll_output_bytes": MAX_POLL_OUTPUT_BYTES, "wait_milliseconds": MAX_WAIT_MS},
    postconditions={
        "output": "bounded-incremental",
        "process_control": False,
        "wait_return": "output-or-state-or-terminal-deadline-by-mode",
    },
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
    cwd: str
    argv: list[str]
    last_activity: float
    created_at: float = 0.0
    start_identity: str | None = None
    process: subprocess.Popen[bytes] | None = None
    master_fd: int = -1
    base_cursor: int = 0
    output: bytearray = field(default_factory=bytearray)
    status: str = "running"
    lifecycle: str = "RUNNING"
    termination_reason: str | None = None
    exit_code: int | None = None
    completed_at: float | None = None
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
        running_hard_wall_seconds: float = RUNNING_HARD_WALL_SECONDS,
        completed_retention_seconds: float = COMPLETED_RETENTION_SECONDS,
        reaper_interval: float = _REAPER_INTERVAL_SECONDS,
        max_active_sessions: int | None = None,
        admission: HeavyExecutionAdmission | None = None,
        start_reaper: bool = True,
    ) -> None:
        self._clock = clock
        self._running_hard_wall_seconds = float(running_hard_wall_seconds)
        self._completed_retention_seconds = float(completed_retention_seconds)
        self._reaper_interval = float(reaper_interval)
        self.max_active_sessions = (
            effective_session_limit()
            if max_active_sessions is None
            else _validated_session_limit(max_active_sessions)
        )
        self._admission = heavy_execution_admission() if admission is None else admission
        self._sessions: dict[str, _Session] = {}
        self._start_identities: dict[str, str] = {}
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

    def start(
        self,
        argv: list[str],
        cwd: str,
        start_identity: str | None = None,
    ) -> dict[str, Any]:
        checked_identity = self._validated_start_identity(start_identity)
        checked_argv = _validated_argv(argv)
        _PROTECTED_GUARD.check(checked_argv, tool_name="terminal_start")
        checked_cwd = str(_validated_cwd(cwd, _workspace_root()))
        exact_spec = (checked_cwd, tuple(checked_argv))
        now = self._clock()

        capacity_error: RuntimeStateError | None = None
        with self._lock:
            self._evict_completed_locked(now)
            if checked_identity is not None:
                existing_id = self._start_identities.get(checked_identity)
                if existing_id is not None:
                    existing = self._sessions.get(existing_id)
                    if existing is None:
                        self._start_identities.pop(checked_identity, None)
                    else:
                        existing_spec = (existing.cwd, tuple(existing.argv))
                        if existing_spec != exact_spec:
                            raise RuntimeStateError(
                                "START_IDENTITY_CONFLICT: start_identity is already bound to a different cwd/argv"
                            )
                        return self._session_result(existing, cursor=0)

            session = _Session(
                session_id=self._new_session_id_locked(),
                cwd=checked_cwd,
                argv=list(checked_argv),
                created_at=now,
                last_activity=now,
                start_identity=checked_identity,
                status="starting",
                lifecycle="STARTING",
                timing_context=current_call_context(),
            )
            self._sessions[session.session_id] = session
            if checked_identity is not None:
                self._start_identities[checked_identity] = session.session_id

            active = sum(
                retained.status in {"starting", "running"}
                for retained in self._sessions.values()
            )
            if active > self.max_active_sessions:
                if checked_identity is None:
                    self._remove_session_locked(session.session_id)
                else:
                    self._mark_pre_effect_locked(session, now)
                    self._sessions.pop(session.session_id)
                    self._sessions[session.session_id] = session
                    self._evict_completed_locked(now)
                capacity_error = RuntimeStateError(
                    f"configured maximum {self.max_active_sessions} active terminal sessions reached"
                )

        if capacity_error is not None:
            raise capacity_error

        try:
            lease = self._admission.acquire()
        except BaseException:
            if checked_identity is None:
                with self._lock:
                    self._remove_session_locked(session.session_id)
            else:
                self._terminalize_pre_effect(session)
            raise
        with session.cleanup_lock:
            session.heavy_lease = lease

        master_fd = slave_fd = -1
        process: subprocess.Popen[bytes] | None = None
        try:
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                checked_argv,
                cwd=checked_cwd,
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
            if checked_identity is None:
                self._release_lease(session)
                with self._lock:
                    self._remove_session_locked(session.session_id)
            else:
                self._terminalize_pre_effect(session)
            raise

        os.close(slave_fd)
        with session.cleanup_lock:
            session.process = process
            session.master_fd = master_fd
            session.process_started_wall = time.time()
            session.process_started_mono = time.monotonic()
            with session.changed:
                session.status = "running"
                session.lifecycle = "RUNNING"
                session.changed.notify_all()

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
            self._cleanup_process(session, "start_failed_post_effect")
            if checked_identity is None:
                with self._lock:
                    self._remove_session_locked(session.session_id)
            raise
        return self.poll(session_id=session.session_id, cursor=0, wait_ms=0)

    def poll(
        self,
        session_id: str | None = None,
        cursor: int = 0,
        wait_ms: int = 0,
        start_identity: str | None = None,
        wait_for: str = "output_or_state",
    ) -> dict[str, Any]:
        session = self._resolve_session(session_id=session_id, start_identity=start_identity)
        checked_cursor = self._validated_cursor(cursor)
        checked_wait_ms = self._validated_wait_ms(wait_ms)
        checked_wait_for = self._validated_wait_for(wait_for)

        self._touch(session)
        if checked_wait_ms:
            deadline = time.monotonic() + checked_wait_ms / 1000.0
            with session.changed:
                if checked_wait_for == "output_or_state":
                    if (
                        session.status == "running"
                        and checked_cursor == session.base_cursor + len(session.output)
                    ):
                        session.changed.wait(max(0.0, deadline - time.monotonic()))
                else:
                    while session.status != "exited":
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        session.changed.wait(remaining)
        return self._session_result(session, cursor=checked_cursor)

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
                process = session.process
                if process is None:
                    raise RuntimeStateError("terminal session is not running")
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                self._touch(session)
                return self._control_result(session)

        if action == "terminate":
            self._require_no_arguments(data, rows, cols)
            self._cleanup_process(session, "explicit_terminate")
            return self._control_result(session)

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

    def reap_once(self) -> list[str]:
        now = self._clock()
        with self._lock:
            sessions = list(self._sessions.values())
        affected: list[str] = []
        for session in sessions:
            if session.status in {"starting", "running"}:
                if now - session.created_at < self._running_hard_wall_seconds:
                    continue
                if session.process is None:
                    if session.start_identity is not None:
                        self._terminalize_pre_effect(session)
                    else:
                        self._release_lease(session)
                        with self._lock:
                            self._remove_session_locked(session.session_id)
                else:
                    self._cleanup_process(session, "hard_wall_timeout")
                affected.append(session.session_id)

        with self._lock:
            before = set(self._sessions)
            self._evict_completed_locked(now)
            removed = before - set(self._sessions)
        affected.extend(sorted(removed))
        return affected

    def reap_idle_once(self) -> list[str]:
        """Compatibility shim for internal callers; reaping is hard-wall/retention based."""
        return self.reap_once()

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
            if session.process is not None and not session.finalized:
                self._cleanup_process(session, "shutdown", retain=False)
            else:
                self._release_lease(session)
        with self._lock:
            self._sessions.clear()
            self._start_identities.clear()

    def _resolve_session(
        self,
        *,
        session_id: str | None,
        start_identity: str | None,
    ) -> _Session:
        if (session_id is None) == (start_identity is None):
            raise RuntimeValidationError(
                "terminal_poll requires exactly one of session_id or start_identity"
            )
        if start_identity is not None:
            checked_identity = self._validated_start_identity(start_identity)
            if checked_identity is None:
                raise RuntimeValidationError("START_IDENTITY_UNKNOWN")
            with self._lock:
                mapped = self._start_identities.get(checked_identity)
                session = None if mapped is None else self._sessions.get(mapped)
            if session is None:
                raise RuntimeValidationError("START_IDENTITY_UNKNOWN")
            return session
        return self._get_session(session_id)

    def _get_session(self, session_id: str | None) -> _Session:
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
        process = session.process
        if process is None:
            return
        process.wait()
        self._cleanup_process(session, "natural_exit")

    def _cleanup_process(
        self,
        session: _Session,
        termination_state: str = "natural_exit",
        *,
        retain: bool = True,
    ) -> bool:
        # Lock order: per-session cleanup_lock -> session.lock -> manager._lock.
        with session.cleanup_lock:
            if session.finalized:
                return False
            process = session.process
            if process is None:
                return False
            _terminate_process_group(process)
            session.reader_done.wait(_READER_DRAIN_SECONDS)
            with session.changed:
                session.exit_code = process.returncode
                session.status = "exited"
                session.lifecycle = (
                    "START_FAILED_POST_EFFECT"
                    if termination_state == "start_failed_post_effect"
                    else "COMPLETED"
                )
                session.termination_reason = termination_state
                session.completed_at = self._clock()
                try:
                    if session.master_fd >= 0:
                        os.close(session.master_fd)
                except OSError:
                    pass
                session.finalized = True
                session.changed.notify_all()

            self._release_lease(session)
            if retain:
                self._retain_completed_session(session)

            emit_process_end(
                session.timing_context,
                tool_name="terminal_start",
                process_kind="persistent_pty",
                started_wall=session.process_started_wall,
                started_mono=session.process_started_mono,
                termination_state=termination_state,
            )
            return True

    def _terminalize_pre_effect(self, session: _Session) -> None:
        with session.cleanup_lock:
            if session.finalized:
                return
            with session.changed:
                session.status = "exited"
                session.lifecycle = "START_FAILED_PRE_EFFECT"
                session.termination_reason = "start_failed_pre_effect"
                session.completed_at = self._clock()
                session.finalized = True
                session.changed.notify_all()
            self._release_lease(session)
            self._retain_completed_session(session)

    @staticmethod
    def _mark_pre_effect_locked(session: _Session, now: float) -> None:
        session.status = "exited"
        session.lifecycle = "START_FAILED_PRE_EFFECT"
        session.termination_reason = "start_failed_pre_effect"
        session.completed_at = now
        session.finalized = True

    def _retain_completed_session(self, session: _Session) -> None:
        with self._lock:
            if self._sessions.get(session.session_id) is not session:
                return
            self._sessions.pop(session.session_id)
            self._sessions[session.session_id] = session
            self._evict_completed_locked(self._clock())

    def _evict_completed_locked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, retained in self._sessions.items()
            if retained.status == "exited"
            and retained.completed_at is not None
            and now - retained.completed_at >= self._completed_retention_seconds
        ]
        for session_id in expired:
            self._remove_session_locked(session_id)

        completed_ids = [
            session_id
            for session_id, retained in self._sessions.items()
            if retained.status == "exited"
        ]
        excess = len(completed_ids) - MAX_RETAINED_COMPLETED_SESSIONS
        for session_id in completed_ids[: max(0, excess)]:
            self._remove_session_locked(session_id)

    def _remove_session_locked(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if (
            session is not None
            and session.start_identity is not None
            and self._start_identities.get(session.start_identity) == session_id
        ):
            self._start_identities.pop(session.start_identity, None)

    def _release_lease(self, session: _Session) -> None:
        lease = session.heavy_lease
        if lease is not None:
            lease.release()
            session.heavy_lease = None

    def _session_result(self, session: _Session, *, cursor: int) -> dict[str, Any]:
        with session.changed:
            retained_end = session.base_cursor + len(session.output)
            if cursor > retained_end:
                raise RuntimeValidationError("cursor is ahead of available session output")
            cursor_expired = cursor < session.base_cursor
            dropped = max(0, session.base_cursor - cursor)
            start_cursor = max(cursor, session.base_cursor)
            start_index = start_cursor - session.base_cursor
            raw = bytes(session.output[start_index : start_index + MAX_POLL_OUTPUT_BYTES])
            result: dict[str, Any] = {
                "session_id": session.session_id,
                "status": session.status,
                "lifecycle": session.lifecycle,
                "output": raw.decode("utf-8", errors="replace"),
                "next_cursor": start_cursor + len(raw),
                "cursor_expired": cursor_expired,
                "dropped_output_bytes": dropped,
            }
            if session.start_identity is not None:
                result["start_identity"] = session.start_identity
            if session.status == "exited":
                result["exit_code"] = session.exit_code
                result["termination_reason"] = session.termination_reason
            return result

    def _touch(self, session: _Session) -> None:
        with session.cleanup_lock:
            session.last_activity = self._clock()

    def _new_session_id_locked(self) -> str:
        while True:
            candidate = secrets.token_hex(8)
            if candidate not in self._sessions:
                return candidate

    @staticmethod
    def _validated_start_identity(start_identity: str | None) -> str | None:
        if start_identity is None:
            return None
        if not isinstance(start_identity, str) or re.fullmatch(r"[0-9a-f]{32}", start_identity) is None:
            raise RuntimeValidationError(
                "start_identity must be exactly 32 lowercase hexadecimal characters"
            )
        return start_identity

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
    def _validated_wait_for(wait_for: str) -> str:
        if not isinstance(wait_for, str) or wait_for not in {
            "output_or_state",
            "terminal_or_deadline",
        }:
            raise RuntimeValidationError(
                "wait_for must be output_or_state or terminal_or_deadline"
            )
        return wait_for

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
            process = session.process
            if (
                session.status != "running"
                or process is None
                or process.poll() is not None
            ):
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
            self.reap_once()


_MANAGER = TerminalSessionManager()
atexit.register(_MANAGER.shutdown)


def start_terminal(
    argv: list[str],
    cwd: str,
    start_identity: str | None = None,
) -> dict[str, Any]:
    return _MANAGER.start(argv, cwd, start_identity)


def poll_terminal(
    session_id: str | None = None,
    cursor: int = 0,
    wait_ms: int = 0,
    start_identity: str | None = None,
    wait_for: str = "output_or_state",
) -> dict[str, Any]:
    return _MANAGER.poll(
        session_id=session_id,
        cursor=cursor,
        wait_ms=wait_ms,
        start_identity=start_identity,
        wait_for=wait_for,
    )


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
