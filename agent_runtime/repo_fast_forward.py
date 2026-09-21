from __future__ import annotations

import os
import re
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import RepoFastForwardResult, TypedToolErrorCode
from .tool_contract import Authority, MutationAuthority, NetworkAuthority, ToolAnnotations, ToolClass, ToolContract

GIT_EXECUTABLE = "/usr/bin/git"
CALL_DEADLINE_SECONDS = 30.0
_BRANCH_MAX_CHARS = 255
_TEXT_MAX_BYTES = 8 * 1024
_STATUS_MAX_BYTES = 8 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_OPERATION_PATHS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "rebase-merge",
    "rebase-apply",
    "BISECT_START",
)

REPO_FAST_FORWARD_CONTRACT = ToolContract(
    name="repo_fast_forward",
    tool_class=ToolClass.REPO,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, True, True),
    preconditions={
        "cwd": "exact-clean-nonbare-repository-root-inside-workspace",
        "branch": "attached-current-branch-with-origin-upstream",
        "expected_local_head": "exact-bound-commit",
        "expected_remote_head": "exact-bound-commit",
    },
    bounds={"branch_chars": _BRANCH_MAX_CHARS, "deadline_milliseconds": int(CALL_DEADLINE_SECONDS * 1000)},
    postconditions={"remote": "origin-only", "fetch": "bound-branch-only", "mutation": "exact-fast-forward-only", "post_state": "clean"},
)


class RepoFastForwardFailure(Exception):
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
    output_truncated: bool


@dataclass(frozen=True, slots=True)
class _LocalState:
    branch: str
    head: str
    upstream: str


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RepoFastForwardFailure(
            "DEADLINE_EXCEEDED",
            "repo_fast_forward exceeded its fixed call deadline",
            retryable=True,
        )
    return remaining


def _git_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("HOME", "PATH", "TMPDIR", "XDG_CONFIG_HOME", "SSH_AUTH_SOCK"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.update(
        {
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


def _git_argv(args: list[str], *, disable_hooks: bool = False) -> list[str]:
    argv = [
        GIT_EXECUTABLE,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "color.ui=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
    ]
    if disable_hooks:
        argv.extend(["-c", "core.hooksPath=/dev/null"])
    argv.extend(args)
    return argv


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
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
    max_stdout_bytes: int = _TEXT_MAX_BYTES,
    disable_hooks: bool = False,
) -> _GitResult:
    _remaining(deadline)
    argv = _git_argv(args, disable_hooks=disable_hooks)
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_env(),
            shell=False,
        )
    except OSError as exc:
        raise RepoFastForwardFailure(
            "INTERNAL_ERROR",
            "unable to start fixed Git executable",
        ) from exc

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    truncated = False
    try:
        while selector.get_map():
            remaining = _remaining(deadline)
            events = selector.select(timeout=min(remaining, 0.1))
            if not events:
                if process.poll() is not None:
                    continue
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except OSError as exc:
                    _terminate_process(process)
                    raise RepoFastForwardFailure(
                        "TRANSIENT_FAILURE",
                        "fixed Git output could not be read",
                        retryable=True,
                    ) from exc
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                allowed = max_stdout_bytes - len(output)
                if allowed <= 0:
                    truncated = True
                    _terminate_process(process)
                    break
                output.extend(chunk[:allowed])
                if len(chunk) > allowed:
                    truncated = True
                    _terminate_process(process)
                    break
            if truncated:
                break

        if process.poll() is None:
            try:
                process.wait(timeout=_remaining(deadline))
            except subprocess.TimeoutExpired as exc:
                _terminate_process(process)
                raise RepoFastForwardFailure(
                    "DEADLINE_EXCEEDED",
                    "repo_fast_forward exceeded its fixed call deadline",
                    retryable=True,
                ) from exc
    except RepoFastForwardFailure:
        _terminate_process(process)
        raise
    finally:
        selector.close()
        process.stdout.close()

    return _GitResult(
        returncode=int(process.returncode if process.returncode is not None else -1),
        stdout=bytes(output),
        output_truncated=truncated,
    )


def _text(result: _GitResult) -> str:
    if result.output_truncated:
        raise RepoFastForwardFailure(
            "TRANSIENT_FAILURE",
            "bounded Git metadata exceeded its output limit",
            retryable=True,
        )
    return result.stdout.decode("utf-8", "replace").strip()


def _workspace_root() -> Path:
    raw = os.environ.get("AGENT_RUNTIME_WORKSPACE_ROOT", "")
    if not raw:
        raise RepoFastForwardFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is unavailable",
        )
    path = Path(raw)
    if not path.is_absolute():
        raise RepoFastForwardFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is invalid",
        )
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise RepoFastForwardFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is unavailable",
        ) from exc
    if not stat.S_ISDIR(mode):
        raise RepoFastForwardFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is invalid",
        )
    return resolved


def _validate_cwd(raw_cwd: str, workspace_root: Path) -> Path:
    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            "cwd must be a non-empty absolute path",
        )
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise RepoFastForwardFailure("INVALID_ARGUMENT", "cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            "cwd must identify an existing directory",
        ) from exc
    if not stat.S_ISDIR(mode):
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            "cwd must identify an existing directory",
        )
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise RepoFastForwardFailure(
            "OUTSIDE_WORKSPACE",
            "cwd resolves outside AGENT_RUNTIME_WORKSPACE_ROOT",
        ) from exc
    return resolved


def _validate_sha(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            f"{field_name} must be an exact lowercase 40-hex commit SHA",
        )


def _validate_branch(repo: Path, branch: str, deadline: float) -> None:
    if (
        not isinstance(branch, str)
        or not branch
        or len(branch) > _BRANCH_MAX_CHARS
        or branch.startswith("-")
    ):
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            "branch must be a bounded legal Git branch name",
        )
    result = _run_git(
        repo,
        ["check-ref-format", "--branch", branch],
        deadline=deadline,
    )
    if result.returncode != 0 or result.output_truncated:
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            "branch must be a bounded legal Git branch name",
        )


def _require_repository_root(cwd: Path, deadline: float) -> Path:
    bare = _run_git(cwd, ["rev-parse", "--is-bare-repository"], deadline=deadline)
    if bare.returncode != 0:
        raise RepoFastForwardFailure(
            "NOT_GIT_REPOSITORY",
            "cwd is not a supported Git working tree",
        )
    if _text(bare) == "true":
        raise RepoFastForwardFailure(
            "NOT_GIT_REPOSITORY",
            "bare Git repositories are not supported",
        )

    top = _run_git(cwd, ["rev-parse", "--show-toplevel"], deadline=deadline)
    if top.returncode != 0:
        raise RepoFastForwardFailure(
            "NOT_GIT_REPOSITORY",
            "cwd is not a supported Git working tree",
        )
    try:
        root = Path(_text(top)).resolve(strict=True)
    except OSError as exc:
        raise RepoFastForwardFailure(
            "NOT_GIT_REPOSITORY",
            "Git repository root is unavailable",
        ) from exc
    if root != cwd:
        raise RepoFastForwardFailure(
            "NOT_REPOSITORY_ROOT",
            "cwd must identify the Git repository root",
        )

    shallow = _run_git(root, ["rev-parse", "--is-shallow-repository"], deadline=deadline)
    if shallow.returncode != 0:
        raise RepoFastForwardFailure(
            "TRANSIENT_FAILURE",
            "unable to verify Git shallow state",
            retryable=True,
        )
    if _text(shallow) == "true":
        raise RepoFastForwardFailure(
            "INVALID_ARGUMENT",
            "shallow Git repositories are not supported",
        )
    return root


def _operation_in_progress(repo: Path, deadline: float) -> bool:
    for name in _OPERATION_PATHS:
        path_result = _run_git(
            repo,
            ["rev-parse", "--git-path", name],
            deadline=deadline,
        )
        if path_result.returncode != 0:
            raise RepoFastForwardFailure(
                "TRANSIENT_FAILURE",
                "unable to inspect Git operation state",
                retryable=True,
            )
        path = Path(_text(path_result))
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return True
    return False


def _require_clean(repo: Path, deadline: float) -> None:
    status = _run_git(
        repo,
        ["status", "--porcelain=v2", "-z", "--untracked-files=normal"],
        deadline=deadline,
        max_stdout_bytes=_STATUS_MAX_BYTES,
    )
    if status.stdout or status.output_truncated:
        raise RepoFastForwardFailure(
            "DIRTY_WORKTREE",
            "repository must have no tracked, staged, untracked, or conflicted changes",
        )
    if status.returncode != 0:
        raise RepoFastForwardFailure(
            "TRANSIENT_FAILURE",
            "unable to verify repository cleanliness",
            retryable=True,
        )


def _read_local_state(repo: Path, deadline: float) -> _LocalState:
    if _operation_in_progress(repo, deadline):
        raise RepoFastForwardFailure(
            "OPERATION_IN_PROGRESS",
            "repository has an in-progress Git operation",
        )
    _require_clean(repo, deadline)

    branch_result = _run_git(
        repo,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        deadline=deadline,
    )
    if branch_result.returncode == 1:
        raise RepoFastForwardFailure(
            "DETACHED_HEAD",
            "repository HEAD must be attached to the requested branch",
        )
    if branch_result.returncode != 0:
        raise RepoFastForwardFailure(
            "TRANSIENT_FAILURE",
            "unable to read current Git branch",
            retryable=True,
        )
    current_branch = _text(branch_result)

    head_result = _run_git(
        repo,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        deadline=deadline,
    )
    if head_result.returncode != 0:
        raise RepoFastForwardFailure(
            "TRANSIENT_FAILURE",
            "unable to read current Git HEAD",
            retryable=True,
        )
    head = _text(head_result)

    upstream_result = _run_git(
        repo,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        deadline=deadline,
    )
    upstream = _text(upstream_result) if upstream_result.returncode == 0 else ""
    return _LocalState(branch=current_branch, head=head, upstream=upstream)


def _validate_bound_state(
    state: _LocalState,
    branch: str,
    expected_local_head: str,
    expected_remote_head: str,
) -> None:
    if state.branch != branch:
        raise RepoFastForwardFailure(
            "BRANCH_MISMATCH",
            "current Git branch does not match the requested branch",
        )
    if state.upstream != f"origin/{branch}":
        raise RepoFastForwardFailure(
            "UPSTREAM_MISMATCH",
            "current branch upstream must equal origin/<branch>",
        )
    if state.head not in {expected_local_head, expected_remote_head}:
        raise RepoFastForwardFailure(
            "LOCAL_HEAD_MISMATCH",
            "current Git HEAD does not match either expected bound commit",
        )


def _fetch_expected_branch(repo: Path, branch: str, deadline: float) -> None:
    result = _run_git(
        repo,
        [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        deadline=deadline,
    )
    if result.returncode != 0 or result.output_truncated:
        raise RepoFastForwardFailure(
            "FETCH_FAILED",
            "fixed origin branch fetch failed",
            retryable=True,
        )


def _tracking_head(repo: Path, branch: str, deadline: float) -> str:
    result = _run_git(
        repo,
        ["rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"],
        deadline=deadline,
    )
    if result.returncode != 0 or result.output_truncated:
        raise RepoFastForwardFailure(
            "REMOTE_HEAD_MISMATCH",
            "origin tracking head does not match the expected remote commit",
        )
    return _text(result)


def _require_expected_tracking_head(
    repo: Path,
    branch: str,
    expected_remote_head: str,
    deadline: float,
) -> str:
    tracking_head = _tracking_head(repo, branch, deadline)
    if tracking_head != expected_remote_head:
        raise RepoFastForwardFailure(
            "REMOTE_HEAD_MISMATCH",
            "origin tracking head does not match the expected remote commit",
        )
    return tracking_head


def _require_ancestor(
    repo: Path,
    expected_local_head: str,
    expected_remote_head: str,
    deadline: float,
) -> None:
    result = _run_git(
        repo,
        ["merge-base", "--is-ancestor", expected_local_head, expected_remote_head],
        deadline=deadline,
    )
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise RepoFastForwardFailure(
            "NON_FAST_FORWARD",
            "expected local commit is not an ancestor of expected remote commit",
        )
    raise RepoFastForwardFailure(
        "TRANSIENT_FAILURE",
        "unable to verify fast-forward ancestry",
        retryable=True,
    )


def _require_unchanged_local_state(
    repo: Path,
    initial: _LocalState,
    deadline: float,
) -> _LocalState:
    try:
        current = _read_local_state(repo, deadline)
    except RepoFastForwardFailure as exc:
        if exc.code in {
            "DIRTY_WORKTREE",
            "OPERATION_IN_PROGRESS",
            "DETACHED_HEAD",
        }:
            raise RepoFastForwardFailure(
                "LOCAL_STATE_CHANGED",
                "local repository state changed after fetch; re-observe before retry",
            ) from None
        raise
    if current != initial:
        raise RepoFastForwardFailure(
            "LOCAL_STATE_CHANGED",
            "local repository state changed after fetch; re-observe before retry",
        )
    return current


def _postcondition_state(
    repo: Path,
    branch: str,
    expected_remote_head: str,
    deadline: float,
) -> tuple[_LocalState, str]:
    try:
        state = _read_local_state(repo, deadline)
        tracking_head = _tracking_head(repo, branch, deadline)
    except RepoFastForwardFailure as exc:
        raise RepoFastForwardFailure(
            "FAST_FORWARD_FAILED",
            "fast-forward postcondition is ambiguous; re-observe before another consequence",
        ) from exc
    if (
        state.branch != branch
        or state.upstream != f"origin/{branch}"
        or state.head != expected_remote_head
        or tracking_head != expected_remote_head
    ):
        raise RepoFastForwardFailure(
            "FAST_FORWARD_FAILED",
            "fast-forward postcondition is ambiguous; re-observe before another consequence",
        )
    return state, tracking_head


def fast_forward_repository(
    cwd: str,
    branch: str,
    expected_local_head: str,
    expected_remote_head: str,
) -> RepoFastForwardResult:
    workspace_root = _workspace_root()
    validated_cwd = _validate_cwd(cwd, workspace_root)
    _validate_sha(expected_local_head, "expected_local_head")
    _validate_sha(expected_remote_head, "expected_remote_head")

    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    repo = _require_repository_root(validated_cwd, deadline)
    _validate_branch(repo, branch, deadline)
    initial = _read_local_state(repo, deadline)
    _validate_bound_state(
        initial,
        branch,
        expected_local_head,
        expected_remote_head,
    )

    _fetch_expected_branch(repo, branch, deadline)
    _require_expected_tracking_head(
        repo,
        branch,
        expected_remote_head,
        deadline,
    )
    _require_ancestor(
        repo,
        expected_local_head,
        expected_remote_head,
        deadline,
    )
    _require_unchanged_local_state(repo, initial, deadline)

    did_fast_forward = False
    if initial.head == expected_local_head and expected_local_head != expected_remote_head:
        result = _run_git(
            repo,
            ["merge", "--ff-only", "--no-edit", expected_remote_head],
            deadline=deadline,
            disable_hooks=True,
        )
        if result.returncode != 0 or result.output_truncated:
            raise RepoFastForwardFailure(
                "FAST_FORWARD_FAILED",
                "fast-forward failed; re-observe before another consequence",
            )
        did_fast_forward = True

    final_state, tracking_head = _postcondition_state(
        repo,
        branch,
        expected_remote_head,
        deadline,
    )
    return RepoFastForwardResult(
        schema_version=1,
        status="fast_forwarded" if did_fast_forward else "already_at_target",
        repository_root=str(repo),
        branch=branch,
        remote="origin",
        upstream=f"origin/{branch}",
        expected_local_head=expected_local_head,
        expected_remote_head=expected_remote_head,
        head_before=initial.head,
        head_after=final_state.head,
        tracking_head=tracking_head,
        fetched=True,
        network_used=True,
        fast_forwarded=did_fast_forward,
        deadline_seconds=CALL_DEADLINE_SECONDS,
    )
