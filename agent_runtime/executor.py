from __future__ import annotations

import os
import secrets
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from .contracts import ARGV_ITEM_MAX_BYTES, ARGV_MAX_ITEMS, ARGV_TOTAL_MAX_BYTES
from .errors import RuntimeValidationError
from .protection import _PROTECTED_GUARD
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
_TERMINATE_GRACE_SECONDS = 0.5
_PRESERVED_ENV_NAMES = ("PATH", "HOME", "USER", "TMPDIR", "LANG")

TERMINAL_EXEC_CONTRACT = ToolContract(
    name="terminal_exec",
    tool_class=ToolClass.PROCESS,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, True, True),
    preconditions={
        "cwd": "validated-workspace-descendant",
        "argv": "literal-nonempty-shell-false-protected-runtime-filtered",
        "stdin": "disconnected",
        "start_identity": "required-exactly-32-lowercase-hex",
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
        "output": "bounded-separate-streams",
        "lifecycle": "shared-keyed-pipe-core",
        "duplicate": "join-original-deadline-and-retained-result",
    },
)


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
        raise RuntimeValidationError("cwd must be a non-empty absolute path", reason_code="INVALID_CWD")
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise RuntimeValidationError("cwd must be an absolute path", reason_code="INVALID_CWD")
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
        from .tool_contract import ContractErrorCode

        raise RuntimeValidationError(
            "cwd resolves outside AGENT_RUNTIME_WORKSPACE_ROOT",
            code=ContractErrorCode.OUTSIDE_WORKSPACE,
            reason_code="OUTSIDE_WORKSPACE",
        ) from exc
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


def execute_terminal(
    argv: list[str],
    cwd: str,
    timeout_seconds: float = 300.0,
    start_identity: str | None = None,
) -> dict[str, Any]:
    """Internal facade over the shared pipe core.

    The MCP surface requires a caller-supplied identity. This helper generates a
    one-call key only for legacy in-process callers and tests.
    """

    from .session import execute_terminal as execute_shared

    identity = start_identity or secrets.token_hex(16)
    return execute_shared(argv, cwd, identity, _validated_timeout(timeout_seconds))
