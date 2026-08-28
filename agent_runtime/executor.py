from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

WORKSPACE_ROOT_ENV = "AGENT_RUNTIME_WORKSPACE_ROOT"
MAX_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 8192
_TERMINATE_GRACE_SECONDS = 0.5
_PRESERVED_ENV_NAMES = ("PATH", "HOME", "USER", "TMPDIR", "LANG")


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
        raise ValueError("argv must be a non-empty list of strings")
    if any(not isinstance(item, str) for item in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if not argv[0]:
        raise ValueError("argv executable must be non-empty")
    if any("\x00" in item for item in argv):
        raise ValueError("argv must not contain NUL bytes")
    return list(argv)


def _validated_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be numeric")
    timeout = float(timeout_seconds)
    if timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return timeout


def _workspace_root() -> Path:
    raw = os.environ.get(WORKSPACE_ROOT_ENV, "")
    if not raw:
        raise ValueError(f"{WORKSPACE_ROOT_ENV} must be set")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{WORKSPACE_ROOT_ENV} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{WORKSPACE_ROOT_ENV} must identify an existing directory") from exc
    if not resolved.is_dir():
        raise ValueError(f"{WORKSPACE_ROOT_ENV} must identify an existing directory")
    return resolved


def _validated_cwd(raw_cwd: str, root: Path) -> Path:
    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise ValueError("cwd must be a non-empty absolute path")
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise ValueError("cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("cwd must identify an existing directory") from exc
    if not resolved.is_dir():
        raise ValueError("cwd must identify an existing directory")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("cwd resolves outside AGENT_RUNTIME_WORKSPACE_ROOT") from exc
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
    timeout = _validated_timeout(timeout_seconds)
    root = _workspace_root()
    checked_cwd = _validated_cwd(cwd, root)

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
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_capture = _BoundedCapture(MAX_OUTPUT_BYTES)
    stderr_capture = _BoundedCapture(MAX_OUTPUT_BYTES)
    stdout_thread = threading.Thread(
        target=stdout_capture.consume,
        args=(process.stdout,),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stderr_capture.consume,
        args=(process.stderr,),
        daemon=True,
    )
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

    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)
    if stdout_thread.is_alive():
        process.stdout.close()
        stdout_thread.join(timeout=1.0)
    if stderr_thread.is_alive():
        process.stderr.close()
        stderr_thread.join(timeout=1.0)

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
