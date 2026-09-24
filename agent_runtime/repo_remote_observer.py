from __future__ import annotations

import os
import re
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import RepoRemoteObserverResult, RepoRemoteRef, TypedToolErrorCode
from .repo_fast_forward import GIT_EXECUTABLE, _git_argv, _git_env
from .tool_contract import (
    Authority,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

CALL_DEADLINE_SECONDS = 10.0
REMOTE_BRANCH_MAX = 1000
REMOTE_STDOUT_MAX_BYTES = 256 * 1024
STDERR_MAX_BYTES = 16 * 1024
LOCAL_STDOUT_MAX_BYTES = 16 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

REPO_REMOTE_OBSERVER_CONTRACT = ToolContract(
    name="repo_remote_observer",
    tool_class=ToolClass.REPO,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.NONE),
    annotations=ToolAnnotations(True, False, True, True),
    preconditions={
        "cwd": "exact-nonbare-repository-root-inside-workspace",
        "branch": "attached-current-branch",
        "remote": "fixed-origin-only",
    },
    bounds={
        "branch_entries": REMOTE_BRANCH_MAX,
        "stdout_bytes": REMOTE_STDOUT_MAX_BYTES,
        "stderr_bytes": STDERR_MAX_BYTES,
        "deadline_milliseconds": int(CALL_DEADLINE_SECONDS * 1000),
    },
    postconditions={
        "remote": "origin-only",
        "fetch": False,
        "network_used": True,
        "local_refs_mutated": False,
        "graph_relation": "unknown",
    },
)


class RepoRemoteObserverFailure(Exception):
    def __init__(
        self,
        code: TypedToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:256]
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RepoRemoteObserverFailure(
            "DEADLINE_EXCEEDED",
            "repo_remote_observer exceeded its fixed call deadline",
            retryable=True,
        )
    return remaining


def _workspace_root() -> Path:
    raw = os.environ.get("AGENT_RUNTIME_WORKSPACE_ROOT", "")
    if not raw:
        raise RepoRemoteObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is unavailable",
        )
    path = Path(raw)
    if not path.is_absolute():
        raise RepoRemoteObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is invalid",
        )
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise RepoRemoteObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is unavailable",
        ) from exc
    if not stat.S_ISDIR(mode):
        raise RepoRemoteObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is invalid",
        )
    return resolved


def _validate_cwd(raw_cwd: str, workspace_root: Path) -> Path:
    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise RepoRemoteObserverFailure(
            "INVALID_ARGUMENT",
            "cwd must be a non-empty absolute path",
        )
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise RepoRemoteObserverFailure("INVALID_ARGUMENT", "cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise RepoRemoteObserverFailure(
            "INVALID_ARGUMENT",
            "cwd must identify an existing directory",
        ) from exc
    if not stat.S_ISDIR(mode):
        raise RepoRemoteObserverFailure(
            "INVALID_ARGUMENT",
            "cwd must identify an existing directory",
        )
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise RepoRemoteObserverFailure(
            "OUTSIDE_WORKSPACE",
            "cwd resolves outside AGENT_RUNTIME_WORKSPACE_ROOT",
        ) from exc
    return resolved


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
            return


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    deadline: float,
    stdout_limit: int,
) -> _GitResult:
    _remaining(deadline)
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
        raise RepoRemoteObserverFailure(
            "INTERNAL_ERROR",
            "unable to start fixed Git executable",
        ) from exc

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": STDERR_MAX_BYTES}
    try:
        while selector.get_map():
            events = selector.select(timeout=min(_remaining(deadline), 0.1))
            if not events:
                if process.poll() is not None:
                    continue
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except OSError as exc:
                    _terminate(process)
                    raise RepoRemoteObserverFailure(
                        "TRANSIENT_FAILURE",
                        "fixed Git output could not be read",
                        retryable=True,
                    ) from exc
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream = key.data
                target = buffers[stream]
                limit = limits[stream]
                if len(target) + len(chunk) > limit:
                    _terminate(process)
                    raise RepoRemoteObserverFailure(
                        "OUTPUT_LIMIT",
                        f"fixed Git {stream} exceeded its hard output limit",
                    )
                target.extend(chunk)

        if process.poll() is None:
            try:
                process.wait(timeout=_remaining(deadline))
            except subprocess.TimeoutExpired as exc:
                _terminate(process)
                raise RepoRemoteObserverFailure(
                    "DEADLINE_EXCEEDED",
                    "repo_remote_observer exceeded its fixed call deadline",
                    retryable=True,
                ) from exc
    except RepoRemoteObserverFailure:
        _terminate(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    return _GitResult(
        returncode=int(process.returncode if process.returncode is not None else -1),
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
    )


def _decode_stdout(result: _GitResult) -> str:
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoRemoteObserverFailure(
            "TRANSIENT_FAILURE",
            "fixed Git stdout was not valid UTF-8",
            retryable=True,
        ) from exc


def _require_repository_root(cwd: Path, deadline: float) -> Path:
    inside = _run_git(
        cwd,
        ["rev-parse", "--is-inside-work-tree"],
        deadline=deadline,
        stdout_limit=LOCAL_STDOUT_MAX_BYTES,
    )
    if inside.returncode != 0 or _decode_stdout(inside).strip() != "true":
        raise RepoRemoteObserverFailure(
            "NOT_GIT_REPOSITORY",
            "cwd is not a supported Git working tree",
        )
    bare = _run_git(
        cwd,
        ["rev-parse", "--is-bare-repository"],
        deadline=deadline,
        stdout_limit=LOCAL_STDOUT_MAX_BYTES,
    )
    if bare.returncode != 0 or _decode_stdout(bare).strip() == "true":
        raise RepoRemoteObserverFailure(
            "NOT_GIT_REPOSITORY",
            "bare Git repositories are not supported",
        )
    top = _run_git(
        cwd,
        ["rev-parse", "--show-toplevel"],
        deadline=deadline,
        stdout_limit=LOCAL_STDOUT_MAX_BYTES,
    )
    if top.returncode != 0:
        raise RepoRemoteObserverFailure(
            "NOT_GIT_REPOSITORY",
            "Git repository root is unavailable",
        )
    try:
        root = Path(_decode_stdout(top).strip()).resolve(strict=True)
    except OSError as exc:
        raise RepoRemoteObserverFailure(
            "NOT_GIT_REPOSITORY",
            "Git repository root is unavailable",
        ) from exc
    if root != cwd:
        raise RepoRemoteObserverFailure(
            "NOT_REPOSITORY_ROOT",
            "cwd must identify the Git repository root",
        )
    return root


def _local_branch_and_head(repo: Path, deadline: float) -> tuple[str, str]:
    branch = _run_git(
        repo,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        deadline=deadline,
        stdout_limit=LOCAL_STDOUT_MAX_BYTES,
    )
    if branch.returncode != 0:
        raise RepoRemoteObserverFailure(
            "DETACHED_HEAD",
            "repository must have an attached current branch",
        )
    local_branch = _decode_stdout(branch).strip()
    if not local_branch:
        raise RepoRemoteObserverFailure(
            "DETACHED_HEAD",
            "repository must have an attached current branch",
        )
    head = _run_git(
        repo,
        ["rev-parse", "--verify", "HEAD"],
        deadline=deadline,
        stdout_limit=LOCAL_STDOUT_MAX_BYTES,
    )
    local_head = _decode_stdout(head).strip()
    if head.returncode != 0 or _SHA_RE.fullmatch(local_head) is None:
        raise RepoRemoteObserverFailure(
            "TRANSIENT_FAILURE",
            "current Git HEAD could not be resolved exactly",
            retryable=True,
        )
    return local_branch, local_head


def _parse_remote_refs(stdout: bytes) -> list[RepoRemoteRef]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoRemoteObserverFailure(
            "TRANSIENT_FAILURE",
            "fixed origin branch output was not valid UTF-8",
            retryable=True,
        ) from exc

    refs: list[RepoRemoteRef] = []
    seen: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line:
            raise RepoRemoteObserverFailure(
                "TRANSIENT_FAILURE",
                "fixed origin branch output contained an empty record",
                retryable=True,
            )
        fields = raw_line.split("\t")
        if len(fields) != 2:
            raise RepoRemoteObserverFailure(
                "TRANSIENT_FAILURE",
                "fixed origin branch output was malformed",
                retryable=True,
            )
        sha, full_ref = fields
        prefix = "refs/heads/"
        if _SHA_RE.fullmatch(sha) is None or not full_ref.startswith(prefix):
            raise RepoRemoteObserverFailure(
                "TRANSIENT_FAILURE",
                "fixed origin branch output was malformed",
                retryable=True,
            )
        name = full_ref[len(prefix):]
        if not name:
            raise RepoRemoteObserverFailure(
                "TRANSIENT_FAILURE",
                "fixed origin branch output was malformed",
                retryable=True,
            )
        if name in seen:
            raise RepoRemoteObserverFailure(
                "TRANSIENT_FAILURE",
                "fixed origin branch output contained duplicate or contradictory refs",
                retryable=True,
            )
        seen[name] = sha
        refs.append(RepoRemoteRef(name=name, sha=sha))
        if len(refs) > REMOTE_BRANCH_MAX:
            raise RepoRemoteObserverFailure(
                "OUTPUT_LIMIT",
                "fixed origin branch count exceeded the hard exactness limit",
            )
    return refs


def observe_remote_repository(cwd: str) -> RepoRemoteObserverResult:
    workspace_root = _workspace_root()
    validated_cwd = _validate_cwd(cwd, workspace_root)
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    repo = _require_repository_root(validated_cwd, deadline)
    local_branch, local_head = _local_branch_and_head(repo, deadline)

    remote = _run_git(
        repo,
        ["ls-remote", "--symref", "--branches", "origin"],
        deadline=deadline,
        stdout_limit=REMOTE_STDOUT_MAX_BYTES,
    )
    if remote.returncode != 0:
        raise RepoRemoteObserverFailure(
            "TRANSIENT_FAILURE",
            "fresh fixed-origin branch observation failed",
            retryable=True,
        )

    remote_branches = _parse_remote_refs(remote.stdout)
    remote_by_name = {item.name: item.sha for item in remote_branches}
    remote_branch_head = remote_by_name.get(local_branch)
    return RepoRemoteObserverResult(
        schema_version=1,
        repository_root=str(repo),
        local_branch=local_branch,
        local_head=local_head,
        remote="origin",
        remote_branch_head=remote_branch_head,
        remote_branch_exists=remote_branch_head is not None,
        remote_branches=remote_branches,
        branch_count=len(remote_branches),
        fetched=False,
        network_used=True,
        local_refs_mutated=False,
        ahead=None,
        behind=None,
        deadline_seconds=CALL_DEADLINE_SECONDS,
    )
