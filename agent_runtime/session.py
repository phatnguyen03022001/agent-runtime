from __future__ import annotations

import atexit
import codecs
import errno
import fcntl
import os
import pty
import re
import secrets
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .capacity import HeavyExecutionAdmission, HeavyExecutionLease, heavy_execution_admission
from .contracts import (
    ARGV_ITEM_MAX_BYTES,
    ARGV_MAX_ITEMS,
    ARGV_TOTAL_MAX_BYTES,
    TERMINAL_DATA_MAX_BYTES,
    TERMINAL_POLL_MAX_OUTPUT_BYTES,
)
from .durable_pipe import (
    DURABLE_ROOT_ENV,
    DURABLE_SPEC_SCHEMA_VERSION,
    DurableControlPending,
    DurableOwnerLost,
    DurableRecoveryOverCapacity,
    DurableSnapshot,
    DurableStateCorrupt,
    DurableStore,
    durable_job_id,
    execution_spec_digest,
    verify_process_identity,
)
from .errors import (
    RuntimeCapacityError,
    RuntimeStateError,
    RuntimeValidationError,
    annotate_failure,
)
from .executor import (
    WORKSPACE_ROOT_ENV,
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
    ContractErrorCode,
    EffectState,
    SafeNextAction,
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
MAX_POLL_OUTPUT_BYTES = TERMINAL_POLL_MAX_OUTPUT_BYTES
MAX_RETAINED_COMPLETED_SESSIONS = 16
MAX_WAIT_MS = 30000
MAX_EXEC_TIMEOUT_SECONDS = 3600.0
DEFAULT_EXEC_TIMEOUT_SECONDS = 300.0
_READ_CHUNK_BYTES = 8192
_READER_DRAIN_SECONDS = 0.2
_DURABLE_FINALIZATION_GRACE_SECONDS = 2.0
_REAPER_INTERVAL_SECONDS = 1.0

def _complete_utf8_prefix_length(data: bytes, *, final: bool) -> int:
    """Return the longest prefix that does not end inside a valid code point."""

    if final:
        return len(data)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    decoder.decode(data, final=False)
    pending, _ = decoder.getstate()
    return len(data) - len(pending)


TERMINAL_START_CONTRACT = ToolContract(
    name="terminal_start",
    tool_class=ToolClass.PROCESS,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, False, True),
    preconditions={
        "cwd": "validated-workspace-descendant",
        "argv": "literal-nonempty-shell-false-protected-runtime-filtered",
        "mode": "pty-default-or-pipe",
        "start_identity": "optional-for-pty-required-for-pipe-exactly-32-lowercase-hex",
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
        "process_group": True,
        "lifecycle": "shared-managed-execution-core",
        "keyed_start": "reserve-before-popen-idempotent-within-runtime-incarnation",
        "pipe_stdin": "closed",
        "pipe_output": "separate-streams",
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
        "output": "incremental-or-none-default-incremental",
        "max_output_bytes": "raw-byte-budget-0-through-hard-poll-limit",
    },
    bounds={
        "poll_output_bytes": MAX_POLL_OUTPUT_BYTES,
        "max_output_bytes": MAX_POLL_OUTPUT_BYTES,
        "wait_milliseconds": MAX_WAIT_MS,
    },
    postconditions={
        "output": "bounded-incremental-or-status-only-without-cursor-consumption",
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
    mode: str = "pty"
    durability: str = "process"
    entry_surface: str = "terminal_start"
    timeout_seconds: float | None = None
    process: subprocess.Popen[bytes] | None = None
    master_fd: int = -1
    base_cursor: int = 0
    output: bytearray = field(default_factory=bytearray)
    output_chunks: list[tuple[str, bytes]] = field(default_factory=list)
    retained_output_bytes: int = 0
    stdout_capture: _BoundedPipeCapture | None = None
    stderr_capture: _BoundedPipeCapture | None = None
    readers_remaining: int = 0
    pipe_reader_failed: bool = False
    pipe_closing: bool = False
    cleanup_failed: bool = False
    deadline_mono: float | None = None
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
    spec_digest: str | None = None
    durable_job_id: str | None = None
    durable_created_at_epoch: float | None = None
    durable_hard_wall_deadline_epoch: float | None = None
    durable_fault_reason: str | None = None
    durable_timing_emitted: bool = False

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)


class _BoundedPipeCapture:
    """Keep one pipe stream bounded while the shared poll cursor keeps order."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def consume(self, chunk: bytes) -> None:
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


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
        durable_state_root: str | Path | None = None,
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
        self._admission = admission
        self._sessions: dict[str, _Session] = {}
        self._start_identities: dict[str, str] = {}
        self._durable_store = DurableStore(durable_state_root)
        self._durable_recovery_reason: str | None = None
        self._durable_scan_fault: str | None = None
        self._lock = threading.RLock()
        self._stop_reaper = threading.Event()
        self._reaper_thread: threading.Thread | None = None
        self._recover_durable_jobs()
        if start_reaper:
            self._reaper_thread = threading.Thread(
                target=self._reaper_loop,
                name="agent-runtime-session-reaper",
                daemon=True,
            )
            self._reaper_thread.start()

    def active_session_count(self) -> int:
        """Return the lock-protected count of starting or running sessions."""

        with self._lock:
            return sum(
                session.status in {"starting", "running"}
                for session in self._sessions.values()
            )

    def recovery_reason(self) -> str | None:
        return self._durable_recovery_reason

    def _recover_durable_jobs(self) -> None:
        try:
            job_ids = self._durable_store.scan_job_ids()
        except DurableRecoveryOverCapacity:
            self._durable_recovery_reason = "DURABLE_RECOVERY_OVER_CAPACITY"
            return
        except DurableStateCorrupt:
            self._durable_scan_fault = "DURABLE_STATE_CORRUPT"
            return

        recovered: list[tuple[_Session, bool]] = []
        for job_id in job_ids:
            try:
                snapshot = self._durable_store.read_snapshot(job_id)
            except DurableStateCorrupt:
                self._durable_scan_fault = "DURABLE_STATE_CORRUPT"
                continue
            state = snapshot.state
            if state["status"] == "exited":
                completed = state["completed_at_epoch"]
                if (
                    isinstance(completed, (int, float))
                    and not isinstance(completed, bool)
                    and time.time() - float(completed) >= self._completed_retention_seconds
                ):
                    try:
                        if self._durable_store.remove_completed(job_id):
                            continue
                    except DurableStateCorrupt:
                        self._durable_scan_fault = "DURABLE_STATE_CORRUPT"
            session = self._session_from_durable_snapshot(job_id, snapshot)
            owner_valid = True
            if session.status in {"starting", "running"}:
                owner_valid = self._snapshot_owner_valid(snapshot)
                if not owner_valid:
                    session.durable_fault_reason = "DURABLE_OWNER_LOST"
            recovered.append((session, owner_valid))

        admission = self._admission or heavy_execution_admission()
        valid_running = sum(
            session.status in {"starting", "running"} and owner_valid
            for session, owner_valid in recovered
        )
        total_running = sum(
            session.status in {"starting", "running"}
            for session, _owner_valid in recovered
        )
        if total_running > self.max_active_sessions:
            self._durable_recovery_reason = "DURABLE_RECOVERY_OVER_CAPACITY"
        elif valid_running and admission.active + valid_running > admission.limit:
            self._durable_recovery_reason = "DURABLE_RECOVERY_OVER_CAPACITY"

        for session, owner_valid in recovered:
            if session.session_id in self._sessions:
                self._durable_scan_fault = "DURABLE_STATE_CORRUPT"
                continue
            if session.status in {"starting", "running"} and owner_valid:
                try:
                    session.heavy_lease = admission.acquire()
                except RuntimeCapacityError:
                    self._durable_recovery_reason = "DURABLE_RECOVERY_OVER_CAPACITY"
            self._sessions[session.session_id] = session
            if session.start_identity is not None:
                prior = self._start_identities.get(session.start_identity)
                if prior is not None and prior != session.session_id:
                    self._durable_scan_fault = "DURABLE_STATE_CORRUPT"
                    session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
                else:
                    self._start_identities[session.start_identity] = session.session_id

    def _session_from_durable_snapshot(
        self,
        job_id: str,
        snapshot: DurableSnapshot,
    ) -> _Session:
        state = snapshot.state
        now = self._clock()
        session = _Session(
            session_id=state["session_id"],
            cwd="",
            argv=[],
            created_at=now,
            last_activity=now,
            start_identity=state["start_identity"],
            mode="pipe",
            durability="runtime_restart",
            entry_surface="terminal_start",
            status=state["status"],
            lifecycle=state["lifecycle"],
            spec_digest=state["spec_digest"],
            durable_job_id=job_id,
            durable_created_at_epoch=float(state["created_at_epoch"]),
            durable_hard_wall_deadline_epoch=float(state["hard_wall_deadline_epoch"]),
        )
        self._apply_durable_snapshot(session, snapshot)
        return session

    @staticmethod
    def _snapshot_owner_valid(snapshot: DurableSnapshot) -> bool:
        state = snapshot.state
        return (
            verify_process_identity(state["runner_identity"])
            and verify_process_identity(state["process_identity"])
        )

    def _apply_durable_snapshot(
        self,
        session: _Session,
        snapshot: DurableSnapshot,
    ) -> None:
        state = snapshot.state
        if (
            state["session_id"] != session.session_id
            or state["start_identity"] != session.start_identity
            or state["spec_digest"] != session.spec_digest
            or state["durability"] != "runtime_restart"
            or state["mode"] != "pipe"
        ):
            raise DurableStateCorrupt("durable recovery identity binding changed")
        with session.changed:
            session.base_cursor = int(state["base_cursor"])
            session.output_chunks = list(snapshot.chunks)
            session.retained_output_bytes = int(state["retained_output_bytes"])
            session.status = state["status"]
            session.lifecycle = state["lifecycle"]
            session.termination_reason = state["termination_reason"]
            session.exit_code = state["exit_code"]
            session.durable_created_at_epoch = float(state["created_at_epoch"])
            session.durable_hard_wall_deadline_epoch = float(state["hard_wall_deadline_epoch"])
            session.changed.notify_all()

    @staticmethod
    def _raise_durable_unknown(reason_code: str, message: str) -> None:
        error = RuntimeStateError(
            message,
            code=(
                ContractErrorCode.INTERNAL_ERROR
                if reason_code == "DURABLE_STATE_CORRUPT"
                else ContractErrorCode.UNAVAILABLE
            ),
            reason_code=reason_code,
        )
        annotate_failure(
            error,
            code=error.code,
            reason_code=reason_code,
            message=message,
            retryable=False,
            effect_state=EffectState.UNKNOWN,
            reconciliation_required=True,
            safe_next_action=SafeNextAction.RECONCILE,
        )
        raise error

    def _raise_if_recovery_blocked(self) -> None:
        if self._durable_recovery_reason is None:
            return
        error = RuntimeStateError(
            "durable Runtime recovery exceeds configured execution capacity",
            code=ContractErrorCode.UNAVAILABLE,
            reason_code=self._durable_recovery_reason,
        )
        annotate_failure(
            error,
            code=ContractErrorCode.UNAVAILABLE,
            reason_code=self._durable_recovery_reason,
            message="durable Runtime recovery exceeds configured execution capacity",
            retryable=False,
            effect_state=EffectState.ABSENT,
            reconciliation_required=True,
            safe_next_action=SafeNextAction.RECONCILE,
        )
        raise error

    def _refresh_durable_session(self, session: _Session) -> DurableSnapshot:
        if session.durable_fault_reason is not None:
            self._raise_durable_unknown(
                session.durable_fault_reason,
                "durable job ownership or state cannot be established",
            )
        job_id = session.durable_job_id
        if job_id is None:
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable job identifier is unavailable",
            )
        try:
            snapshot = self._durable_store.read_snapshot(job_id)
        except DurableStateCorrupt:
            session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable job state is corrupt or incomplete",
            )
        previous_status = session.status
        if snapshot.state["status"] in {"starting", "running"} and not self._snapshot_owner_valid(snapshot):
            transition_deadline = (
                time.monotonic() + _DURABLE_FINALIZATION_GRACE_SECONDS
            )
            while time.monotonic() < transition_deadline:
                time.sleep(0.02)
                try:
                    candidate = self._durable_store.read_snapshot(job_id)
                except DurableStateCorrupt:
                    continue
                snapshot = candidate
                if snapshot.state["status"] == "exited" or self._snapshot_owner_valid(snapshot):
                    break
            if snapshot.state["status"] in {"starting", "running"} and not self._snapshot_owner_valid(snapshot):
                session.durable_fault_reason = "DURABLE_OWNER_LOST"
                self._raise_durable_unknown(
                    "DURABLE_OWNER_LOST",
                    "durable job owner identity no longer matches the persisted process instance",
                )
        try:
            self._apply_durable_snapshot(session, snapshot)
        except DurableStateCorrupt:
            session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable recovery identity binding changed",
            )
        if previous_status != "exited" and session.status == "exited":
            self._release_lease(session)
            if session.timing_context is not None and not session.durable_timing_emitted:
                emit_process_end(
                    session.timing_context,
                    tool_name="terminal_start",
                    process_kind="durable_pipe",
                    started_wall=session.process_started_wall,
                    started_mono=session.process_started_mono,
                    termination_state=session.termination_reason or "start_failed_post_effect",
                )
                session.durable_timing_emitted = True
        return snapshot

    def _adopt_durable_snapshot(
        self,
        job_id: str,
        snapshot: DurableSnapshot,
    ) -> _Session:
        session = self._session_from_durable_snapshot(job_id, snapshot)
        with self._lock:
            existing = self._sessions.get(session.session_id)
            if existing is not None:
                if (
                    existing.start_identity != session.start_identity
                    or existing.spec_digest != session.spec_digest
                    or existing.durability != "runtime_restart"
                ):
                    self._raise_durable_unknown(
                        "DURABLE_STATE_CORRUPT",
                        "durable recovered session collides with an existing session",
                    )
                return existing
            active = sum(
                retained.status in {"starting", "running"}
                for retained in self._sessions.values()
            )
            if session.status in {"starting", "running"} and active >= self.max_active_sessions:
                self._durable_recovery_reason = "DURABLE_RECOVERY_OVER_CAPACITY"
            owner_valid = (
                session.status == "exited"
                or self._snapshot_owner_valid(snapshot)
            )
            if session.status in {"starting", "running"} and not owner_valid:
                session.durable_fault_reason = "DURABLE_OWNER_LOST"
            elif session.status in {"starting", "running"}:
                try:
                    session.heavy_lease = (self._admission or heavy_execution_admission()).acquire()
                except RuntimeCapacityError:
                    self._durable_recovery_reason = "DURABLE_RECOVERY_OVER_CAPACITY"
            self._sessions[session.session_id] = session
            if session.start_identity is not None:
                prior = self._start_identities.get(session.start_identity)
                if prior is not None and prior != session.session_id:
                    session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
                else:
                    self._start_identities[session.start_identity] = session.session_id
        return session

    def _load_existing_durable_identity(
        self,
        start_identity: str,
        spec_digest: str,
    ) -> _Session | None:
        try:
            exists = self._durable_store.job_exists_for_identity(start_identity)
        except DurableStateCorrupt:
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable identity path cannot be validated",
            )
        if not exists:
            return None
        try:
            snapshot = self._durable_store.read_for_identity(start_identity)
        except DurableStateCorrupt:
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable identity exists but its state is corrupt or incomplete",
            )
        if snapshot.state["spec_digest"] != spec_digest:
            raise RuntimeStateError(
                "START_IDENTITY_CONFLICT: start_identity is already bound to a different execution specification",
                reason_code="START_IDENTITY_CONFLICT",
            )
        session = self._adopt_durable_snapshot(
            durable_job_id(start_identity),
            snapshot,
        )
        if session.durable_fault_reason is not None:
            self._raise_durable_unknown(
                session.durable_fault_reason,
                "durable identity exists but owner continuity cannot be established",
            )
        return session

    def _dispatch_durable(
        self,
        session: _Session,
        checked_argv: list[str],
        checked_cwd: str,
    ) -> dict[str, Any]:
        assert session.start_identity is not None
        assert session.spec_digest is not None
        job_id = durable_job_id(session.start_identity)
        session.durable_job_id = job_id
        hard_wall_ms = int(round(self._running_hard_wall_seconds * 1000.0))
        spec = {
            "schema_version": DURABLE_SPEC_SCHEMA_VERSION,
            "job_id": job_id,
            "session_id": session.session_id,
            "start_identity": session.start_identity,
            "spec_digest": session.spec_digest,
            "cwd": checked_cwd,
            "argv": list(checked_argv),
            "mode": "pipe",
            "durability": "runtime_restart",
            "entry_surface": "terminal_start",
            "hard_wall_ms": hard_wall_ms,
        }
        try:
            self._durable_store.prepare_spec(spec)
        except DurableStateCorrupt:
            session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable state could not be prepared safely",
            )

        child_env = _minimal_child_env()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        workspace_root = os.environ.get(WORKSPACE_ROOT_ENV)
        if workspace_root is None:
            try:
                self._durable_store.abort_pre_dispatch(job_id)
            finally:
                self._terminalize_pre_effect(session)
            raise RuntimeValidationError(f"{WORKSPACE_ROOT_ENV} must be set")
        child_env[WORKSPACE_ROOT_ENV] = workspace_root
        child_env[DURABLE_ROOT_ENV] = str(self._durable_store.root)
        runner_argv = [
            sys.executable,
            "-m",
            "agent_runtime.durable_pipe_runner",
            job_id,
        ]
        session.process_started_wall = time.time()
        session.process_started_mono = time.monotonic()
        try:
            bootstrap = subprocess.Popen(
                runner_argv,
                cwd=str(Path(__file__).resolve().parents[1]),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=False,
                close_fds=True,
            )
        except BaseException as exc:
            try:
                self._durable_store.abort_pre_dispatch(job_id)
            finally:
                self._terminalize_pre_effect(session)
            if isinstance(exc, Exception):
                annotate_failure(
                    exc,
                    code=ContractErrorCode.PRECONDITION_FAILED,
                    reason_code="PROCESS_START_FAILED_PRE_EFFECT",
                    message="durable runner failed before dispatch",
                    retryable=False,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.FIX_REQUEST,
                )
            raise

        try:
            bootstrap.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            session.durable_fault_reason = "DURABLE_OWNER_LOST"
            self._durable_recovery_reason = "DURABLE_OWNER_LOST"
            self._raise_durable_unknown(
                "DURABLE_OWNER_LOST",
                "durable runner bootstrap did not establish ownership in time",
            )

        deadline = time.monotonic() + 5.0
        state_path = self._durable_store.job_path_for_identity(session.start_identity) / "state.json"
        while True:
            if state_path.exists():
                try:
                    snapshot = self._durable_store.read_snapshot(job_id)
                except DurableStateCorrupt:
                    if time.monotonic() < deadline:
                        time.sleep(0.02)
                        continue
                    session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
                    self._raise_durable_unknown(
                        "DURABLE_STATE_CORRUPT",
                        "durable runner did not publish a consistent state",
                    )
                break
            if time.monotonic() >= deadline:
                session.durable_fault_reason = "DURABLE_OWNER_LOST"
                self._durable_recovery_reason = "DURABLE_OWNER_LOST"
                self._raise_durable_unknown(
                    "DURABLE_OWNER_LOST",
                    "durable runner dispatch outcome is unknown",
                )
            time.sleep(0.02)

        if (
            snapshot.state["session_id"] != session.session_id
            or snapshot.state["start_identity"] != session.start_identity
            or snapshot.state["spec_digest"] != session.spec_digest
        ):
            session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable runner published a mismatched identity binding",
            )
        self._apply_durable_snapshot(session, snapshot)
        if session.status in {"starting", "running"} and not self._snapshot_owner_valid(snapshot):
            # A very short target may finalize between the initial state read and
            # process-instance verification. Re-read the atomically finalized
            # state before treating missing ownership as ambiguous.
            self._refresh_durable_session(session)
        if session.status == "exited":
            self._release_lease(session)
        return self._session_result(session, cursor=0)

    def start(
        self,
        argv: list[str],
        cwd: str,
        start_identity: str | None = None,
        mode: str = "pty",
        durability: str = "process",
        *,
        entry_surface: str = "terminal_start",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        checked_identity = self._validated_start_identity(start_identity)
        if not isinstance(mode, str) or mode not in {"pty", "pipe"}:
            raise RuntimeValidationError("mode must be pty or pipe")
        if not isinstance(durability, str) or durability not in {"process", "runtime_restart"}:
            raise RuntimeValidationError("durability must be process or runtime_restart")
        if mode == "pipe" and checked_identity is None:
            raise RuntimeValidationError("pipe mode requires start_identity")
        if durability == "runtime_restart":
            if mode != "pipe":
                raise RuntimeValidationError(
                    "runtime_restart durability requires pipe mode",
                    reason_code="DURABLE_PTY_UNSUPPORTED",
                )
            if checked_identity is None:
                raise RuntimeValidationError("runtime_restart durability requires start_identity")
            if entry_surface != "terminal_start":
                raise RuntimeValidationError(
                    "runtime_restart durability is supported only by terminal_start"
                )
        if entry_surface not in {"terminal_start", "terminal_exec"}:
            raise RuntimeValidationError("unsupported terminal entry surface")
        checked_timeout = (
            self._validated_exec_timeout(timeout_seconds)
            if entry_surface == "terminal_exec"
            else None
        )
        if entry_surface == "terminal_exec" and checked_identity is None:
            raise RuntimeValidationError("terminal_exec requires start_identity")
        if entry_surface == "terminal_exec" and durability != "process":
            raise RuntimeValidationError("terminal_exec is process-local")
        self._raise_if_recovery_blocked()

        checked_argv = _validated_argv(argv)
        _PROTECTED_GUARD.check(checked_argv, tool_name=entry_surface)
        checked_cwd = str(_validated_cwd(cwd, _workspace_root()))
        durable_digest = (
            execution_spec_digest(
                cwd=checked_cwd,
                argv=checked_argv,
                hard_wall_seconds=self._running_hard_wall_seconds,
            )
            if durability == "runtime_restart"
            else None
        )
        exact_spec = (
            checked_cwd,
            tuple(checked_argv),
            mode,
            durability,
            entry_surface,
            checked_timeout,
        )
        now = self._clock()

        capacity_error: RuntimeCapacityError | None = None
        with self._lock:
            self._evict_completed_locked(now)
            if checked_identity is not None:
                existing_id = self._start_identities.get(checked_identity)
                if existing_id is not None:
                    existing = self._sessions.get(existing_id)
                    if existing is None:
                        self._start_identities.pop(checked_identity, None)
                    else:
                        if existing.durability == "runtime_restart":
                            if durability != "runtime_restart" or existing.spec_digest != durable_digest:
                                raise RuntimeStateError(
                                    "START_IDENTITY_CONFLICT: start_identity is already bound to a different execution specification",
                                    reason_code="START_IDENTITY_CONFLICT",
                                )
                            self._refresh_durable_session(existing)
                            return self._session_result(existing, cursor=0)
                        existing_spec = (
                            existing.cwd,
                            tuple(existing.argv),
                            existing.mode,
                            existing.durability,
                            existing.entry_surface,
                            existing.timeout_seconds,
                        )
                        if existing_spec != exact_spec:
                            raise RuntimeStateError(
                                "START_IDENTITY_CONFLICT: start_identity is already bound to a different execution specification",
                                reason_code="START_IDENTITY_CONFLICT",
                            )
                        return self._session_result(existing, cursor=0)

                if durability == "runtime_restart":
                    assert durable_digest is not None
                    recovered = self._load_existing_durable_identity(
                        checked_identity,
                        durable_digest,
                    )
                    if recovered is not None:
                        self._refresh_durable_session(recovered)
                        return self._session_result(recovered, cursor=0)

            session = _Session(
                session_id=self._new_session_id_locked(),
                cwd=checked_cwd,
                argv=list(checked_argv),
                created_at=now,
                last_activity=now,
                start_identity=checked_identity,
                mode=mode,
                durability=durability,
                entry_surface=entry_surface,
                timeout_seconds=checked_timeout,
                status="starting",
                lifecycle="STARTING",
                timing_context=current_call_context(),
                spec_digest=durable_digest,
                durable_job_id=(
                    durable_job_id(checked_identity)
                    if durability == "runtime_restart" and checked_identity is not None
                    else None
                ),
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
                capacity_error = RuntimeCapacityError(
                    f"configured maximum {self.max_active_sessions} active terminal sessions reached"
                )

        if capacity_error is not None:
            raise capacity_error

        try:
            admission = self._admission or heavy_execution_admission()
            lease = admission.acquire()
        except BaseException as exc:
            if checked_identity is None:
                with self._lock:
                    self._remove_session_locked(session.session_id)
            else:
                self._terminalize_pre_effect(session)
            if isinstance(exc, (RuntimeStateError, RuntimeValidationError)):
                raise
            if isinstance(exc, Exception):
                annotate_failure(
                    exc,
                    code=ContractErrorCode.INTERNAL_ERROR,
                    reason_code="ADMISSION_FAILED_PRE_EFFECT",
                    message="terminal admission failed before process dispatch",
                    retryable=False,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.REPORT_DEFECT,
                )
            raise
        with session.cleanup_lock:
            session.heavy_lease = lease

        if durability == "runtime_restart":
            return self._dispatch_durable(session, checked_argv, checked_cwd)

        master_fd = slave_fd = -1
        process: subprocess.Popen[bytes] | None = None
        try:
            if mode == "pty":
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
            else:
                process = subprocess.Popen(
                    checked_argv,
                    cwd=checked_cwd,
                    env=_minimal_child_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                    close_fds=True,
                )
        except BaseException as exc:
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
            if isinstance(exc, Exception):
                annotate_failure(
                    exc,
                    code=ContractErrorCode.PRECONDITION_FAILED,
                    reason_code="PROCESS_START_FAILED_PRE_EFFECT",
                    message="terminal process failed before dispatch",
                    retryable=False,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.FIX_REQUEST,
                )
            raise

        with session.cleanup_lock:
            session.process = process
            session.master_fd = master_fd
            session.process_started_wall = time.time()
            session.process_started_mono = time.monotonic()
            session.deadline_mono = (
                session.process_started_mono + checked_timeout
                if checked_timeout is not None
                else None
            )
            with session.changed:
                session.status = "running"
                session.lifecycle = "RUNNING"
                session.changed.notify_all()
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except OSError as exc:
                self._cleanup_process(session, "start_failed_post_effect")
                annotate_failure(
                    exc,
                    code=ContractErrorCode.INTERNAL_ERROR,
                    reason_code="PROCESS_START_FAILED_POST_EFFECT",
                    message="terminal process dispatched but PTY setup did not complete",
                    retryable=False,
                    effect_state=EffectState.UNKNOWN,
                    reconciliation_required=True,
                    safe_next_action=SafeNextAction.RECONCILE,
                )
                raise

        try:
            if mode == "pty":
                session.readers_remaining = 1
                threading.Thread(
                    target=self._reader,
                    args=(session,),
                    name=f"terminal-reader-{session.session_id}",
                    daemon=True,
                ).start()
            else:
                assert process is not None and process.stdout is not None and process.stderr is not None
                session.stdout_capture = _BoundedPipeCapture(MAX_RETAINED_OUTPUT_BYTES)
                session.stderr_capture = _BoundedPipeCapture(MAX_RETAINED_OUTPUT_BYTES)
                session.readers_remaining = 2
                threading.Thread(
                    target=self._pipe_reader,
                    args=(session, "stdout", process.stdout),
                    name=f"terminal-stdout-reader-{session.session_id}",
                    daemon=True,
                ).start()
                threading.Thread(
                    target=self._pipe_reader,
                    args=(session, "stderr", process.stderr),
                    name=f"terminal-stderr-reader-{session.session_id}",
                    daemon=True,
                ).start()
            threading.Thread(
                target=self._monitor,
                args=(session,),
                name=f"terminal-monitor-{session.session_id}",
                daemon=True,
            ).start()
        except BaseException as exc:
            self._cleanup_process(session, "start_failed_post_effect")
            if checked_identity is None:
                with self._lock:
                    self._remove_session_locked(session.session_id)
            if isinstance(exc, Exception):
                annotate_failure(
                    exc,
                    code=ContractErrorCode.INTERNAL_ERROR,
                    reason_code="PROCESS_START_FAILED_POST_EFFECT",
                    message="terminal process dispatch occurred but start completion was not established",
                    retryable=False,
                    effect_state=EffectState.UNKNOWN,
                    reconciliation_required=True,
                    safe_next_action=SafeNextAction.RECONCILE,
                )
            raise
        return self.poll(session_id=session.session_id, cursor=0, wait_ms=0)

    def execute(
        self,
        argv: list[str],
        cwd: str,
        start_identity: str,
        timeout_seconds: float = DEFAULT_EXEC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Synchronous terminal_exec facade over the shared pipe lifecycle."""

        checked_timeout = self._validated_exec_timeout(timeout_seconds)
        self.start(
            argv,
            cwd,
            start_identity,
            "pipe",
            entry_surface="terminal_exec",
            timeout_seconds=checked_timeout,
        )
        session = self._resolve_session(session_id=None, start_identity=start_identity)
        with session.changed:
            while session.status != "exited" and not session.cleanup_failed:
                deadline = session.deadline_mono
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                session.changed.wait(remaining)
        if session.cleanup_failed and session.status != "exited":
            raise self._post_effect_error(
                "terminal_exec dispatch occurred but process cleanup is incomplete",
                "PROCESS_CLEANUP_FAILED",
            )
        if session.status != "exited":
            self._cleanup_process(session, "timeout")
        if session.lifecycle == "START_FAILED_POST_EFFECT":
            raise self._post_effect_error(
                "terminal_exec dispatch occurred but completion is unknown",
                "PROCESS_START_FAILED_POST_EFFECT",
            )
        if session.lifecycle == "START_FAILED_PRE_EFFECT":
            raise RuntimeStateError(
                "terminal_exec failed before process dispatch",
                reason_code="PROCESS_START_FAILED_PRE_EFFECT",
            )
        if session.stdout_capture is None or session.stderr_capture is None:
            raise self._post_effect_error(
                "terminal_exec dispatch occurred but output capture is unavailable",
                "PROCESS_CAPTURE_UNAVAILABLE",
            )
        if session.exit_code is None:
            raise self._post_effect_error(
                "terminal_exec dispatch occurred but process completion has no exit code",
                "PROCESS_COMPLETION_UNKNOWN",
            )
        return {
            "cwd": session.cwd,
            "argv": list(session.argv),
            "exit_code": session.exit_code,
            "timed_out": session.termination_reason == "timeout",
            "stdout": session.stdout_capture.text(),
            "stderr": session.stderr_capture.text(),
            "stdout_truncated": session.stdout_capture.truncated,
            "stderr_truncated": session.stderr_capture.truncated,
            "start_identity": session.start_identity,
            "session_id": session.session_id,
        }

    def _poll_durable(
        self,
        session: _Session,
        *,
        cursor: int,
        wait_ms: int,
        wait_for: str,
        output: str,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        self._refresh_durable_session(session)
        self._touch(session)
        initial_status = session.status
        initial_end = session.base_cursor + self._retained_output_length(session)
        if wait_ms:
            deadline = time.monotonic() + wait_ms / 1000.0
            while True:
                if wait_for == "terminal_or_deadline":
                    if session.status == "exited":
                        break
                else:
                    current_end = session.base_cursor + self._retained_output_length(session)
                    if session.status != initial_status or current_end != initial_end:
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
                self._refresh_durable_session(session)
        return self._session_result(
            session,
            cursor=cursor,
            output=output,
            max_output_bytes=max_output_bytes,
        )

    def poll(
        self,
        session_id: str | None = None,
        cursor: int = 0,
        wait_ms: int = 0,
        start_identity: str | None = None,
        wait_for: str = "output_or_state",
        output: str = "incremental",
        max_output_bytes: int = MAX_POLL_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        session = self._resolve_session(session_id=session_id, start_identity=start_identity)
        checked_cursor = self._validated_cursor(cursor)
        checked_wait_ms = self._validated_wait_ms(wait_ms)
        checked_wait_for = self._validated_wait_for(wait_for)
        checked_output = self._validated_poll_output(output)
        checked_output_budget = self._validated_poll_output_budget(max_output_bytes)

        if session.durability == "runtime_restart":
            return self._poll_durable(
                session,
                cursor=checked_cursor,
                wait_ms=checked_wait_ms,
                wait_for=checked_wait_for,
                output=checked_output,
                max_output_bytes=checked_output_budget,
            )

        self._touch(session)
        if checked_wait_ms:
            deadline = time.monotonic() + checked_wait_ms / 1000.0
            with session.changed:
                if checked_wait_for == "output_or_state":
                    if (
                        session.status == "running"
                        and checked_cursor == session.base_cursor + self._retained_output_length(session)
                    ):
                        session.changed.wait(max(0.0, deadline - time.monotonic()))
                else:
                    while session.status != "exited":
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        session.changed.wait(remaining)
        return self._session_result(
            session,
            cursor=checked_cursor,
            output=checked_output,
            max_output_bytes=checked_output_budget,
        )

    def _control_durable(
        self,
        session: _Session,
        action: str,
        data: str | None,
        rows: int | None,
        cols: int | None,
    ) -> dict[str, Any]:
        if action == "write":
            self._require_no_dimensions(rows, cols)
            if not isinstance(data, str):
                raise RuntimeValidationError("write action requires UTF-8 string data")
            raise RuntimeValidationError(
                "pipe sessions do not accept input",
                reason_code="PIPE_WRITE_UNSUPPORTED",
            )
        if action == "resize":
            if data is not None:
                raise RuntimeValidationError("resize action does not accept data")
            raise RuntimeValidationError(
                "terminal_resize is supported only for PTY sessions",
                reason_code="PTY_REQUIRED",
            )
        if action not in {"interrupt", "terminate"}:
            raise RuntimeValidationError("action must be one of: write, interrupt, terminate, resize")
        self._require_no_arguments(data, rows, cols)

        snapshot = self._refresh_durable_session(session)
        if session.status != "running":
            raise RuntimeStateError("terminal session is not running")
        state = snapshot.state
        if not self._snapshot_owner_valid(snapshot):
            session.durable_fault_reason = "DURABLE_OWNER_LOST"
            self._raise_durable_unknown(
                "DURABLE_OWNER_LOST",
                "durable control target ownership cannot be verified",
            )
        job_id = session.durable_job_id
        if job_id is None:
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable control job identifier is unavailable",
            )
        try:
            self._durable_store.write_control(job_id, action)
        except DurableControlPending as exc:
            raise RuntimeStateError(
                str(exc),
                code=ContractErrorCode.CONFLICT,
                reason_code="DURABLE_CONTROL_PENDING",
            ) from exc
        except DurableStateCorrupt:
            session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable control state cannot be written safely",
            )

        try:
            confirmed = self._durable_store.read_snapshot(job_id)
            if (
                confirmed.state["runner_identity"] != state["runner_identity"]
                or confirmed.state["process_identity"] != state["process_identity"]
                or not self._snapshot_owner_valid(confirmed)
            ):
                self._durable_store.remove_control(job_id)
                session.durable_fault_reason = "DURABLE_OWNER_LOST"
                self._raise_durable_unknown(
                    "DURABLE_OWNER_LOST",
                    "durable control ownership changed before signal",
                )
            os.kill(int(state["runner_identity"]["pid"]), signal.SIGUSR1)
        except ProcessLookupError:
            try:
                self._durable_store.remove_control(job_id)
            except DurableStateCorrupt:
                pass
            session.durable_fault_reason = "DURABLE_OWNER_LOST"
            self._raise_durable_unknown(
                "DURABLE_OWNER_LOST",
                "durable runner disappeared before control signal",
            )
        except DurableStateCorrupt:
            session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
            self._raise_durable_unknown(
                "DURABLE_STATE_CORRUPT",
                "durable control state changed inconsistently",
            )
        self._touch(session)
        return self._control_result(session)

    def control(
        self,
        session_id: str,
        action: str,
        data: str | None = None,
        rows: int | None = None,
        cols: int | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.durability == "runtime_restart":
            return self._control_durable(session, action, data, rows, cols)
        if action == "write":
            self._require_no_dimensions(rows, cols)
            if not isinstance(data, str):
                raise RuntimeValidationError("write action requires UTF-8 string data")
            if session.mode != "pty":
                raise RuntimeValidationError(
                    "pipe sessions do not accept input",
                    reason_code="PIPE_WRITE_UNSUPPORTED",
                )
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
            if session.mode != "pty":
                raise RuntimeValidationError(
                    "terminal_resize is supported only for PTY sessions",
                    reason_code="PTY_REQUIRED",
                )
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
        wall_now = time.time()
        for session in sessions:
            if session.durability == "runtime_restart":
                if session.status == "starting" and session.lifecycle == "STARTING":
                    continue
                try:
                    self._refresh_durable_session(session)
                except RuntimeStateError:
                    continue
                if session.status == "exited":
                    try:
                        snapshot = self._durable_store.read_snapshot(session.durable_job_id or "")
                        completed = snapshot.state["completed_at_epoch"]
                        if (
                            isinstance(completed, (int, float))
                            and not isinstance(completed, bool)
                            and wall_now - float(completed) >= self._completed_retention_seconds
                            and self._durable_store.remove_completed(session.durable_job_id or "")
                        ):
                            with self._lock:
                                self._remove_session_locked(session.session_id)
                            affected.append(session.session_id)
                    except DurableStateCorrupt:
                        session.durable_fault_reason = "DURABLE_STATE_CORRUPT"
                continue
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
                    try:
                        self._cleanup_process(session, "hard_wall_timeout")
                    except Exception:
                        # Preserve the keyed uncertain session; a later reaper pass may retry cleanup.
                        pass
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
            if session.durability == "runtime_restart":
                self._release_lease(session)
                continue
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
                raise RuntimeValidationError(
                    "START_IDENTITY_UNKNOWN",
                    code=ContractErrorCode.NOT_FOUND,
                    reason_code="START_IDENTITY_UNKNOWN",
                )
            with self._lock:
                mapped = self._start_identities.get(checked_identity)
                session = None if mapped is None else self._sessions.get(mapped)
            if session is None:
                try:
                    if self._durable_store.job_exists_for_identity(checked_identity):
                        snapshot = self._durable_store.read_for_identity(checked_identity)
                        session = self._adopt_durable_snapshot(
                            durable_job_id(checked_identity),
                            snapshot,
                        )
                except DurableStateCorrupt:
                    self._raise_durable_unknown(
                        "DURABLE_STATE_CORRUPT",
                        "durable identity exists but its state cannot be recovered",
                    )
            if session is None:
                raise RuntimeValidationError(
                    "START_IDENTITY_UNKNOWN",
                    code=ContractErrorCode.NOT_FOUND,
                    reason_code="START_IDENTITY_UNKNOWN",
                )
            return session
        return self._get_session(session_id)

    def _get_session(self, session_id: str | None) -> _Session:
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeValidationError("session_id must be a non-empty string")
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            if self._durable_scan_fault is not None:
                self._raise_durable_unknown(
                    self._durable_scan_fault,
                    "durable recovery contains unreadable state and the requested session cannot be reconciled",
                )
            raise RuntimeValidationError(
                "unknown or expired session_id",
                code=ContractErrorCode.NOT_FOUND,
                reason_code="SESSION_UNKNOWN_OR_EXPIRED",
            )
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
            self._reader_finished(session)

    def _pipe_reader(self, session: _Session, stream_name: str, stream: Any) -> None:
        capture = session.stdout_capture if stream_name == "stdout" else session.stderr_capture
        reader_failed = False
        try:
            while True:
                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                except InterruptedError:
                    continue
                except Exception:
                    with session.changed:
                        if not session.pipe_closing:
                            session.pipe_reader_failed = True
                            session.lifecycle = "START_FAILED_POST_EFFECT"
                            reader_failed = True
                        session.changed.notify_all()
                    break
                if not chunk:
                    break
                try:
                    if capture is not None:
                        capture.consume(chunk)
                except Exception:
                    with session.changed:
                        if not session.pipe_closing:
                            session.pipe_reader_failed = True
                            session.lifecycle = "START_FAILED_POST_EFFECT"
                            reader_failed = True
                        session.changed.notify_all()
                    break
                with session.changed:
                    if not session.finalized:
                        self._append_pipe_output_locked(session, stream_name, chunk)
                    session.changed.notify_all()
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            self._reader_finished(session)
        if reader_failed:
            try:
                self._cleanup_process(session, "start_failed_post_effect")
            except Exception:
                # The keyed session retains the post-effect uncertainty for poll/exec.
                pass

    @staticmethod
    def _append_pipe_output_locked(session: _Session, stream_name: str, chunk: bytes) -> None:
        if not chunk:
            return
        session.output_chunks.append((stream_name, chunk))
        session.retained_output_bytes += len(chunk)
        overflow = session.retained_output_bytes - MAX_RETAINED_OUTPUT_BYTES
        while overflow > 0 and session.output_chunks:
            first_stream, first_chunk = session.output_chunks[0]
            if len(first_chunk) <= overflow:
                session.output_chunks.pop(0)
                dropped = len(first_chunk)
            else:
                session.output_chunks[0] = (first_stream, first_chunk[overflow:])
                dropped = overflow
            session.base_cursor += dropped
            session.retained_output_bytes -= dropped
            overflow -= dropped

    @staticmethod
    def _retained_output_length(session: _Session) -> int:
        return session.retained_output_bytes if session.mode == "pipe" else len(session.output)

    @staticmethod
    def _reader_finished(session: _Session) -> None:
        with session.changed:
            if session.readers_remaining > 0:
                session.readers_remaining -= 1
            if session.readers_remaining == 0:
                session.reader_done.set()
            session.changed.notify_all()

    def _monitor(self, session: _Session) -> None:
        process = session.process
        if process is None:
            return
        try:
            deadline = session.deadline_mono
            if deadline is None:
                process.wait()
            else:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                self._cleanup_process(session, "timeout")
            except Exception:
                # Cleanup stores a keyed post-effect failure for synchronous callers.
                pass
            return
        try:
            self._cleanup_process(session, "natural_exit")
        except Exception:
            # Cleanup stores a keyed post-effect failure for synchronous callers.
            return

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
            try:
                _terminate_process_group(process)
            except Exception as exc:
                with session.changed:
                    session.cleanup_failed = True
                    session.lifecycle = "START_FAILED_POST_EFFECT"
                    session.changed.notify_all()
                annotate_failure(
                    exc,
                    code=ContractErrorCode.INTERNAL_ERROR,
                    reason_code="PROCESS_CLEANUP_FAILED",
                    message="terminal process dispatched but process-group cleanup did not complete",
                    retryable=False,
                    effect_state=EffectState.UNKNOWN,
                    reconciliation_required=True,
                    safe_next_action=SafeNextAction.RECONCILE,
                )
                raise
            session.reader_done.wait(_READER_DRAIN_SECONDS)
            if not session.reader_done.is_set() and session.mode == "pipe":
                with session.changed:
                    session.pipe_closing = True
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
                if not session.reader_done.wait(_READER_DRAIN_SECONDS):
                    with session.changed:
                        session.pipe_reader_failed = True
                        session.lifecycle = "START_FAILED_POST_EFFECT"
                        session.changed.notify_all()
            with session.changed:
                if session.pipe_reader_failed:
                    termination_state = "start_failed_post_effect"
                session.exit_code = process.returncode
                session.cleanup_failed = False
                session.status = "exited"
                session.lifecycle = (
                    "START_FAILED_POST_EFFECT"
                    if termination_state == "start_failed_post_effect"
                    else "COMPLETED"
                )
                session.termination_reason = termination_state
                session.completed_at = self._clock()
                try:
                    if session.mode == "pty" and session.master_fd >= 0:
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
                tool_name=session.entry_surface,
                process_kind=(
                    "persistent_pty"
                    if session.mode == "pty" and session.entry_surface == "terminal_start"
                    else "persistent_pipe"
                    if session.entry_surface == "terminal_start"
                    else "one_shot"
                ),
                started_wall=session.process_started_wall,
                started_mono=session.process_started_mono,
                termination_state=(
                    "completed"
                    if session.entry_surface == "terminal_exec" and termination_state == "natural_exit"
                    else "timed_out"
                    if termination_state == "timeout"
                    else termination_state
                ),
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
            if retained.durability == "process"
            and retained.status == "exited"
            and retained.completed_at is not None
            and now - retained.completed_at >= self._completed_retention_seconds
        ]
        for session_id in expired:
            self._remove_session_locked(session_id)

        completed_ids = [
            session_id
            for session_id, retained in self._sessions.items()
            if retained.durability == "process" and retained.status == "exited"
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

    def _session_result(
        self,
        session: _Session,
        *,
        cursor: int,
        output: str = "incremental",
        max_output_bytes: int = MAX_POLL_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        with session.changed:
            retained_output_length = self._retained_output_length(session)
            retained_end = session.base_cursor + retained_output_length
            if cursor > retained_end:
                raise RuntimeValidationError("cursor is ahead of available session output")
            cursor_expired = cursor < session.base_cursor
            dropped = max(0, session.base_cursor - cursor)
            start_cursor = max(cursor, session.base_cursor)
            start_index = start_cursor - session.base_cursor
            result: dict[str, Any] = {
                "session_id": session.session_id,
                "status": session.status,
                "lifecycle": session.lifecycle,
                "mode": session.mode,
                "durability": session.durability,
                "cursor_expired": cursor_expired,
                "dropped_output_bytes": dropped,
            }
            if output == "none":
                result["output"] = ""
                if session.mode == "pipe":
                    result["output_chunks"] = []
                result["next_cursor"] = start_cursor
            elif session.mode == "pty":
                available = bytes(session.output[start_index:])
                raw = available[:max_output_bytes]
                final = session.status == "exited" and len(raw) == len(available)
                emitted_bytes = _complete_utf8_prefix_length(raw, final=final)
                emitted = raw[:emitted_bytes]
                result["output"] = emitted.decode("utf-8", errors="replace")
                result["next_cursor"] = start_cursor + emitted_bytes
            else:
                next_cursor, chunks = self._pipe_output_chunks(
                    session,
                    start_cursor=start_cursor,
                    retained_end=retained_end,
                    max_output_bytes=max_output_bytes,
                )
                result["output"] = ""
                result["output_chunks"] = chunks
                result["next_cursor"] = next_cursor
            if session.start_identity is not None:
                result["start_identity"] = session.start_identity
            if session.status == "exited":
                result["exit_code"] = session.exit_code
                result["termination_reason"] = session.termination_reason
            return result

    @staticmethod
    def _pipe_output_chunks(
        session: _Session,
        *,
        start_cursor: int,
        retained_end: int,
        max_output_bytes: int,
    ) -> tuple[int, list[dict[str, str]]]:
        budget_end = min(retained_end, start_cursor + max_output_bytes)
        if budget_end <= start_cursor:
            return start_cursor, []

        decoders = {
            name: codecs.getincrementaldecoder("utf-8")("replace")
            for name in ("stdout", "stderr")
        }
        safe_end = start_cursor
        offset = session.base_cursor
        for stream_name, data in session.output_chunks:
            chunk_start = offset
            chunk_end = chunk_start + len(data)
            offset = chunk_end
            if chunk_end <= start_cursor or chunk_start >= budget_end:
                continue
            take_start = max(start_cursor, chunk_start)
            take_end = min(budget_end, chunk_end)
            piece = data[take_start - chunk_start : take_end - chunk_start]
            if not piece:
                continue
            decoders[stream_name].decode(piece, final=False)
            if all(not decoder.getstate()[0] for decoder in decoders.values()):
                safe_end = take_end

        include_eof = session.status == "exited" and budget_end == retained_end
        if include_eof:
            # Any unfinished trailing sequence is invalid at EOF and keeps the
            # established replacement-decoding behavior.
            safe_end = retained_end

        output_decoders = {
            name: codecs.getincrementaldecoder("utf-8")("replace")
            for name in ("stdout", "stderr")
        }
        emitted_by_event: dict[int, tuple[str, str]] = {}
        last_event_by_stream: dict[str, int] = {}
        offset = session.base_cursor
        event_index = 0
        for stream_name, data in session.output_chunks:
            chunk_start = offset
            chunk_end = chunk_start + len(data)
            offset = chunk_end
            if chunk_end <= start_cursor or chunk_start >= safe_end:
                continue
            current_event = event_index
            event_index += 1
            take_start = max(start_cursor, chunk_start)
            take_end = min(safe_end, chunk_end)
            piece = data[take_start - chunk_start : take_end - chunk_start]
            if not piece:
                continue
            last_event_by_stream[stream_name] = current_event
            text = output_decoders[stream_name].decode(piece, final=False)
            if text:
                emitted_by_event[current_event] = (stream_name, text)

        if include_eof:
            for stream_name, decoder in output_decoders.items():
                tail = decoder.decode(b"", final=True)
                if tail:
                    current_event = last_event_by_stream.get(stream_name)
                    if current_event is not None:
                        prior = emitted_by_event.get(current_event)
                        emitted_by_event[current_event] = (
                            stream_name,
                            (prior[1] if prior is not None else "") + tail,
                        )

        output_chunks = [
            {"stream": stream_name, "text": text}
            for _event, (stream_name, text) in sorted(emitted_by_event.items())
        ]
        return safe_end, output_chunks

    @staticmethod
    def _post_effect_error(message: str, reason_code: str) -> RuntimeStateError:
        error = RuntimeStateError(message, reason_code=reason_code)
        annotate_failure(
            error,
            code=ContractErrorCode.INTERNAL_ERROR,
            reason_code=reason_code,
            message=message,
            retryable=False,
            effect_state=EffectState.UNKNOWN,
            reconciliation_required=True,
            safe_next_action=SafeNextAction.RECONCILE,
        )
        return error

    @staticmethod
    def _validated_exec_timeout(timeout_seconds: float) -> float:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise RuntimeValidationError("timeout_seconds must be numeric")
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > MAX_EXEC_TIMEOUT_SECONDS:
            raise RuntimeValidationError(
                f"timeout_seconds must be greater than 0 and at most {MAX_EXEC_TIMEOUT_SECONDS:g}"
            )
        return timeout

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
    def _validated_poll_output(output: str) -> str:
        if not isinstance(output, str) or output not in {"incremental", "none"}:
            raise RuntimeValidationError("output must be incremental or none")
        return output

    @staticmethod
    def _validated_poll_output_budget(max_output_bytes: int) -> int:
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes < 0
            or max_output_bytes > MAX_POLL_OUTPUT_BYTES
        ):
            raise RuntimeValidationError(
                f"max_output_bytes must be an integer from 0 to {MAX_POLL_OUTPUT_BYTES}"
            )
        return max_output_bytes

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
    mode: str = "pty",
    durability: str = "process",
) -> dict[str, Any]:
    return _MANAGER.start(argv, cwd, start_identity, mode, durability)


def execute_terminal(
    argv: list[str],
    cwd: str,
    start_identity: str,
    timeout_seconds: float = DEFAULT_EXEC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return _MANAGER.execute(argv, cwd, start_identity, timeout_seconds)


def poll_terminal(
    session_id: str | None = None,
    cursor: int = 0,
    wait_ms: int = 0,
    start_identity: str | None = None,
    wait_for: str = "output_or_state",
    output: str = "incremental",
    max_output_bytes: int = MAX_POLL_OUTPUT_BYTES,
) -> dict[str, Any]:
    return _MANAGER.poll(
        session_id=session_id,
        cursor=cursor,
        wait_ms=wait_ms,
        start_identity=start_identity,
        wait_for=wait_for,
        output=output,
        max_output_bytes=max_output_bytes,
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


def active_terminal_session_count() -> int:
    """Return the active session count owned by the single Runtime manager."""

    return _MANAGER.active_session_count()


def terminal_recovery_reason() -> str | None:
    """Return a stable fail-closed durable recovery reason, if one exists."""

    return _MANAGER.recovery_reason()


def _get_session(session_id: str) -> _Session:
    return _MANAGER._get_session(session_id)
