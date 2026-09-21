from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from .capacity import heavy_execution_admission
from .contracts import ARGV_ITEM_MAX_BYTES, ARGV_MAX_ITEMS, ARGV_TOTAL_MAX_BYTES
from .errors import RuntimeValidationError
from .protection import _PROTECTED_GUARD
from .timing import current_call_context, emit_process_end
from .tool_contract import (
    Authority,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

WORKSPACE_ROOT_ENV = "AGENT_RUNTIME_WORKSPACE_ROOT"
MAX_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 8192
_TERMINATE_GRACE_SECONDS = 0.5
_PRESERVED_ENV_NAMES = ("PATH", "HOME", "USER", "TMPDIR", "LANG")

TERMINAL_EXEC_CONTRACT = ToolContract(
    name="terminal_exec",
    tool_class=ToolClass.PROCESS,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, False, True),
    preconditions={
        "cwd": "validated-workspace-descendant",
        "argv": "literal-nonempty-shell-false-protected-runtime-filtered",
        "stdin": "disconnected",
    },
    bounds={
        "argv_items": ARGV_MAX_ITEMS,
        "argv_item_utf8_bytes": ARGV_ITEM_MAX_BYTES,
        "argv_total_utf8_bytes": ARGV_TOTAL_MAX_BYTES,
        "timeout_milliseconds": int(MAX_TIMEOUT_SECONDS * 1000),
        "stdout_bytes": MAX_OUTPUT_BYTES,
        "stderr_bytes": MAX_OUTPUT_BYTES,
    },
    postconditions={
        "shell": False,
        "process_group_cleanup": "bounded-term-then-kill",
        "output": "bounded",
    },
)


class _ActiveExecutionRegistry:
    """Cleanup ownership for in-flight one-shot process groups only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[subprocess.Popen[bytes], threading.Event] = {}

    def add(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes[process] = threading.Event()

    def discard(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            completed = self._processes.pop(process, None)
        if completed is not None:
            completed.set()

    def shutdown(self) -> None:
        with self._lock:
            processes = tuple(self._processes.items())
        for process, _completed in processes:
            try:
                _terminate_process_group(process)
            except (OSError, subprocess.SubprocessError):
                # Best-effort shutdown must continue to the remaining owned
                # groups; their owning execute_terminal call still finalizes.
                pass
        for _process, completed in processes:
            # execute_terminal owns the bounded pipe drain and lease release.
            # Let it finish before the Runtime restores default signal handling.
            completed.wait(2.0)


_ACTIVE_EXECUTIONS = _ActiveExecutionRegistry()


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.truncated = False

    def consume(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                remaining = self._limit - len(self._data)
                if remaining > 0:
                    self._data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        finally:
            stream.close()

    def text(self) -> str:
        return bytes(self._data).decode("utf-8", errors="replace")


def _validated_argv(argv: list[str]) -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise RuntimeValidationError("argv must be a non-empty list of strings")
    if any(not isinstance(item, str) for item in argv):
        raise RuntimeValidationError("argv must be a non-empty list of strings")
    if not argv[0]:
        raise RuntimeValidationError("argv executable must be non-empty")
    if any("\x00" in item for item in argv):
        raise RuntimeValidationError("argv must not contain NUL bytes")
    return list(argv)


def _validated_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise RuntimeValidationError("timeout_seconds must be numeric")
    timeout = float(timeout_seconds)
    if timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise RuntimeValidationError(
            f"timeout_seconds must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return timeout


def _workspace_root() -> Path:
    raw = os.environ.get(WORKSPACE_ROOT_ENV, "")
    if not raw:
        raise RuntimeValidationError(f"{WORKSPACE_ROOT_ENV} must be set")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeValidationError(f"{WORKSPACE_ROOT_ENV} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeValidationError(f"{WORKSPACE_ROOT_ENV} must identify an existing directory") from exc
    if not resolved.is_dir():
        raise RuntimeValidationError(f"{WORKSPACE_ROOT_ENV} must identify an existing directory")
    return resolved


def _validated_cwd_with_identity(raw_cwd: str, root: Path) -> tuple[Path, tuple[int, int]]:
    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise RuntimeValidationError("cwd must be a non-empty absolute path")
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise RuntimeValidationError("cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        observed = resolved.stat()
    except OSError as exc:
        raise RuntimeValidationError("cwd must identify an existing directory") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise RuntimeValidationError("cwd must identify an existing directory")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeValidationError("cwd resolves outside AGENT_RUNTIME_WORKSPACE_ROOT") from exc
    return resolved, (observed.st_dev, observed.st_ino)


def _validated_cwd(raw_cwd: str, root: Path) -> Path:
    resolved, _identity = _validated_cwd_with_identity(raw_cwd, root)
    return resolved


def _minimal_child_env() -> dict[str, str]:
    child: dict[str, str] = {}
    for name in _PRESERVED_ENV_NAMES:
        value = os.environ.get(name)
        if value is not None:
            child[name] = value
    for name, value in os.environ.items():
        if name.startswith("LC_"):
            child[name] = value
    return child


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    process_group_id = process.pid
    if not _process_group_exists(process_group_id):
        return

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process_group_id):
            break
        time.sleep(0.02)

    process.poll()
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if process.poll() is None:
        process.wait()


def _drain_reader_threads(
    process: subprocess.Popen[bytes],
    stdout_thread: threading.Thread | None,
    stderr_thread: threading.Thread | None,
) -> None:
    """Complete the bounded pipe-reader lifecycle before observing captures."""

    for thread, stream in ((stdout_thread, process.stdout), (stderr_thread, process.stderr)):
        if thread is None:
            continue
        thread.join(timeout=1.0)
        if thread.is_alive() and stream is not None:
            stream.close()
            thread.join(timeout=1.0)


def execute_terminal(
    argv: list[str],
    cwd: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Execute one literal argv under the configured workspace cwd guard.

    The cwd guard selects an allowed working directory. It is not filesystem
    confinement: the chosen executable and its arguments can still access host
    paths according to the operator account's normal permissions.
    """

    checked_argv = _validated_argv(argv)
    _PROTECTED_GUARD.check(checked_argv, tool_name="terminal_exec")
    timeout = _validated_timeout(timeout_seconds)
    root = _workspace_root()
    checked_cwd = _validated_cwd(cwd, root)

    lease = heavy_execution_admission().acquire()
    process: subprocess.Popen[bytes] | None = None
    process_context = None
    process_started_wall = 0.0
    process_started_mono = 0.0
    process_event_emitted = False
    stdout_capture = _BoundedCapture(MAX_OUTPUT_BYTES)
    stderr_capture = _BoundedCapture(MAX_OUTPUT_BYTES)
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    try:
        process = subprocess.Popen(
            checked_argv,
            cwd=str(checked_cwd),
            env=_minimal_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        _ACTIVE_EXECUTIONS.add(process)
        process_context = current_call_context()
        process_started_wall = time.time()
        process_started_mono = time.monotonic()
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(target=stdout_capture.consume, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr_capture.consume, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process)
        else:
            _terminate_process_group(process)

        process_termination = "timed_out" if timed_out else "completed"
        emit_process_end(
            process_context,
            tool_name="terminal_exec",
            process_kind="one_shot",
            started_wall=process_started_wall,
            started_mono=process_started_mono,
            termination_state=process_termination,
        )
        process_event_emitted = True
        _drain_reader_threads(process, stdout_thread, stderr_thread)

        return {
            "cwd": str(checked_cwd),
            "argv": checked_argv,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout_capture.text(),
            "stderr": stderr_capture.text(),
            "stdout_truncated": stdout_capture.truncated,
            "stderr_truncated": stderr_capture.truncated,
        }
    finally:
        try:
            if process is not None:
                if not process_event_emitted:
                    emit_process_end(
                        process_context,
                        tool_name="terminal_exec",
                        process_kind="one_shot",
                        started_wall=process_started_wall,
                        started_mono=process_started_mono,
                        termination_state="error",
                    )
                try:
                    _terminate_process_group(process)
                finally:
                    try:
                        _drain_reader_threads(process, stdout_thread, stderr_thread)
                    finally:
                        _ACTIVE_EXECUTIONS.discard(process)
        finally:
            lease.release()


def shutdown_terminal_executions() -> None:
    """Terminate all Runtime-owned in-flight one-shot process groups."""

    _ACTIVE_EXECUTIONS.shutdown()
