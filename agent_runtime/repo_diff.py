from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path

from .contracts import CapabilityFailure, ReceiptV1Result, RepoDiffResult
from .errors import RuntimeValidationError
from .executor import _validated_cwd, _workspace_root
from .tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
    make_receipt_v1,
)

GIT_EXECUTABLE = "/usr/bin/git"
CALL_DEADLINE_SECONDS = 5.0
FULL_DIFF_MAX_BYTES = 64 * 1024 * 1024
RETURNED_PATCH_MAX_BYTES = 256 * 1024
_METADATA_MAX_BYTES = 64 * 1024
_STDERR_MAX_BYTES = 64 * 1024

REPO_DIFF_CONTRACT = ToolContract(
    name="repo_diff",
    tool_class=ToolClass.REPO,
    authority=Authority(
        workspace_bound=True,
        network=NetworkAuthority.NONE,
        mutation=MutationAuthority.NONE,
    ),
    annotations=ToolAnnotations(
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    ),
    preconditions={
        "cwd": "exact-nonbare-repository-root-inside-workspace",
        "scope": ["worktree", "staged"],
        "git_executable": GIT_EXECUTABLE,
        "shell": False,
    },
    bounds={
        "complete_raw_diff_bytes": FULL_DIFF_MAX_BYTES,
        "returned_patch_bytes": RETURNED_PATCH_MAX_BYTES,
        "deadline_milliseconds": 5000,
    },
    postconditions={
        "network_used": False,
        "untracked_files": "excluded",
        "receipt_kind": "repo-diff",
        "receipt_observed_state": "complete-raw-diff-bytes",
    },
)


def _git_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("HOME", "PATH", "TMPDIR", "XDG_CONFIG_HOME"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.update(
        {
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    workspace = os.environ.get("AGENT_RUNTIME_WORKSPACE_ROOT")
    if workspace:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(workspace).resolve(strict=False))
    return env


def _git_argv(args: list[str]) -> list[str]:
    return [
        GIT_EXECUTABLE,
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "color.ui=false",
        "-c",
        "submodule.recurse=false",
        *args,
    ]


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=0.2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    deadline: float,
    stdout_limit: int,
    limit_reason: str,
) -> tuple[int, bytes, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CapabilityFailure(
            ContractErrorCode.TIMEOUT,
            "DEADLINE_EXCEEDED",
            "repo_diff exceeded its 5-second call deadline",
            retryable=True,
        )
    try:
        process = subprocess.Popen(
            _git_argv(args),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
            shell=False,
        )
    except OSError as exc:
        raise CapabilityFailure(
            ContractErrorCode.UNAVAILABLE,
            "LOCAL_GIT_UNAVAILABLE",
            "fixed local Git executable could not be started",
            retryable=True,
        ) from exc
    assert process.stdout is not None
    assert process.stderr is not None

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise CapabilityFailure(
                    ContractErrorCode.TIMEOUT,
                    "DEADLINE_EXCEEDED",
                    "repo_diff exceeded its 5-second call deadline",
                    retryable=True,
                )
            events = selector.select(timeout=min(remaining, 0.05))
            if not events:
                if process.poll() is not None:
                    for stream in (process.stdout, process.stderr):
                        try:
                            selector.unregister(stream)
                        except KeyError:
                            pass
                    break
                continue
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(stream)
                    except KeyError:
                        pass
                    continue
                if key.data == "stdout":
                    if len(stdout) + len(chunk) > stdout_limit:
                        _terminate(process)
                        raise CapabilityFailure(
                            ContractErrorCode.LIMIT_EXCEEDED,
                            limit_reason,
                            "local Git output exceeded the configured bound",
                        )
                    stdout.extend(chunk)
                else:
                    if len(stderr) < _STDERR_MAX_BYTES:
                        stderr.extend(chunk[: _STDERR_MAX_BYTES - len(stderr)])

        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate(process)
            raise CapabilityFailure(
                ContractErrorCode.TIMEOUT,
                "DEADLINE_EXCEEDED",
                "repo_diff exceeded its 5-second call deadline",
                retryable=True,
            ) from exc
        return returncode, bytes(stdout), bytes(stderr)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            _terminate(process)


def _require_success(
    cwd: Path,
    args: list[str],
    *,
    deadline: float,
    reason: str,
) -> bytes:
    returncode, stdout, _stderr = _run_git(
        cwd,
        args,
        deadline=deadline,
        stdout_limit=_METADATA_MAX_BYTES,
        limit_reason="GIT_METADATA_LIMIT",
    )
    if returncode != 0:
        raise CapabilityFailure(
            ContractErrorCode.PRECONDITION_FAILED,
            reason,
            "cwd is not a supported Git repository state",
        )
    return stdout


def _truncate_patch(raw: bytes) -> tuple[str, bool]:
    text = raw.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) <= RETURNED_PATCH_MAX_BYTES:
        return text, False
    prefix = encoded[:RETURNED_PATCH_MAX_BYTES]
    return prefix.decode("utf-8", errors="ignore"), True


def _validated_repository(cwd: str, *, deadline: float) -> tuple[Path, str]:
    try:
        workspace = _workspace_root()
        checked = _validated_cwd(cwd, workspace)
    except RuntimeValidationError as exc:
        if "outside AGENT_RUNTIME_WORKSPACE_ROOT" in str(exc):
            raise CapabilityFailure(
                ContractErrorCode.OUTSIDE_WORKSPACE,
                "CWD_OUTSIDE_WORKSPACE",
                "cwd resolves outside the configured workspace",
            ) from None
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_CWD",
            "cwd must identify an existing absolute workspace directory",
        ) from None

    top = _require_success(
        checked,
        ["rev-parse", "--show-toplevel"],
        deadline=deadline,
        reason="NOT_GIT_REPOSITORY",
    ).decode("utf-8", errors="strict").strip()
    try:
        repo_root = Path(top).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise CapabilityFailure(
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_GIT_REPOSITORY",
            "Git repository root could not be resolved",
        ) from exc
    if repo_root != checked:
        raise CapabilityFailure(
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_REPOSITORY_ROOT",
            "cwd must be exactly the repository root",
        )

    bare = _require_success(
        checked,
        ["rev-parse", "--is-bare-repository"],
        deadline=deadline,
        reason="NOT_GIT_REPOSITORY",
    ).decode("ascii", errors="strict").strip()
    if bare != "false":
        raise CapabilityFailure(
            ContractErrorCode.PRECONDITION_FAILED,
            "BARE_REPOSITORY",
            "repo_diff requires a non-bare working tree",
        )

    head = _require_success(
        checked,
        ["rev-parse", "--verify", "HEAD"],
        deadline=deadline,
        reason="HEAD_UNAVAILABLE",
    ).decode("ascii", errors="strict").strip()
    if len(head) != 40 or any(ch not in "0123456789abcdef" for ch in head):
        raise CapabilityFailure(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_HEAD_STATE",
            "local Git returned an invalid HEAD identifier",
        )
    return checked, head


def diff_repository(cwd: str, scope: str = "worktree") -> RepoDiffResult:
    """Return a bounded local-only tracked diff plus a full-state ReceiptV1."""

    if scope not in {"worktree", "staged"}:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_SCOPE",
            "scope must be 'worktree' or 'staged'",
        )
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    repo_root, head_sha = _validated_repository(cwd, deadline=deadline)

    diff_args = ["diff"]
    if scope == "staged":
        diff_args.append("--cached")
    diff_args.extend(
        [
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--no-color",
            "--",
        ]
    )
    returncode, raw_diff, _stderr = _run_git(
        repo_root,
        diff_args,
        deadline=deadline,
        stdout_limit=FULL_DIFF_MAX_BYTES,
        limit_reason="DIFF_STATE_LIMIT",
    )
    if returncode != 0:
        raise CapabilityFailure(
            ContractErrorCode.INTERNAL_ERROR,
            "LOCAL_GIT_DIFF_FAILED",
            "local Git diff failed",
            retryable=True,
        )

    receipt = make_receipt_v1(
        kind="repo-diff",
        subject={
            "repository_root": str(repo_root),
            "head_sha": head_sha,
        },
        semantic_parameters={"scope": scope},
        observed_state_bytes=raw_diff,
    )
    patch, patch_truncated = _truncate_patch(raw_diff)
    return RepoDiffResult(
        schema_version=1,
        scope=scope,
        head_sha=head_sha,
        patch=patch,
        patch_truncated=patch_truncated,
        full_diff_bytes=len(raw_diff),
        diff_receipt=ReceiptV1Result(
            schema_version=receipt.schema_version,
            kind="repo-diff",
            digest=receipt.digest,
        ),
        network_used=False,
    )
