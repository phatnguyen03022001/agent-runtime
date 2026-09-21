from __future__ import annotations

import time

from .contracts import RepoPublishResult, TypedToolErrorCode
from .repo_fast_forward import (
    CALL_DEADLINE_SECONDS,
    _BRANCH_MAX_CHARS,
    RepoFastForwardFailure,
    _LocalState,
    _read_local_state,
    _require_expected_tracking_head,
    _require_repository_root,
    _run_git,
    _text,
    _validate_branch,
    _validate_cwd,
    _validate_sha,
    _workspace_root,
)
from .tool_contract import Authority, MutationAuthority, NetworkAuthority, ToolAnnotations, ToolClass, ToolContract

_POST_PUSH_RESERVE_SECONDS = 5.0

REPO_PUBLISH_CONTRACT = ToolContract(
    name="repo_publish",
    tool_class=ToolClass.REPO,
    authority=Authority(True, NetworkAuthority.BOUNDED, MutationAuthority.DESTRUCTIVE),
    annotations=ToolAnnotations(False, True, True, True),
    preconditions={
        "cwd": "exact-clean-nonbare-repository-root-inside-workspace",
        "branch": "attached-current-branch-with-origin-upstream",
        "expected_remote_head": "exact-current-fixed-origin-head",
        "commit": "current-head-and-sole-direct-child-of-expected-remote-head",
    },
    bounds={"branch_chars": _BRANCH_MAX_CHARS, "deadline_milliseconds": int(CALL_DEADLINE_SECONDS * 1000)},
    postconditions={"remote": "origin-only", "push_attempts": 1, "lease": "exact-expected-old", "fresh_remote_postcondition": True},
)


class RepoPublishFailure(Exception):
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


def _translate_failure(exc: RepoFastForwardFailure) -> RepoPublishFailure:
    return RepoPublishFailure(exc.code, exc.message, retryable=exc.retryable)


def _validate_bound_local_state(
    state: _LocalState,
    branch: str,
    commit: str,
) -> None:
    if state.branch != branch:
        raise RepoPublishFailure(
            "BRANCH_MISMATCH",
            "current Git branch does not match the requested branch",
        )
    if state.upstream != f"origin/{branch}":
        raise RepoPublishFailure(
            "UPSTREAM_MISMATCH",
            "current branch upstream must equal origin/<branch>",
        )
    if state.head != commit:
        raise RepoPublishFailure(
            "LOCAL_HEAD_MISMATCH",
            "current Git HEAD does not match the requested commit",
        )
def _require_commit_exists(repo, commit: str, deadline: float) -> None:
    result = _run_git(
        repo,
        ["cat-file", "-e", f"{commit}^{{commit}}"],
        deadline=deadline,
    )
    if result.returncode != 0 or result.output_truncated:
        raise RepoPublishFailure(
            "LOCAL_HEAD_MISMATCH",
            "requested commit is unavailable as a local commit",
        )


def _require_direct_child(
    repo,
    expected_remote_head: str,
    commit: str,
    deadline: float,
) -> None:
    result = _run_git(
        repo,
        ["rev-list", "--parents", "-n", "1", commit],
        deadline=deadline,
    )
    if result.returncode != 0 or result.output_truncated:
        raise RepoPublishFailure(
            "PUBLICATION_LINEAGE_MISMATCH",
            "publication commit lineage could not be proven",
        )
    parts = _text(result).split()
    if (
        len(parts) != 2
        or parts[0] != commit
        or parts[1] != expected_remote_head
    ):
        raise RepoPublishFailure(
            "PUBLICATION_LINEAGE_MISMATCH",
            "publication commit must be the sole direct child of expected_remote_head",
        )


def _parse_remote_head(result, branch: str) -> str | None:
    if result.returncode == 2 and not result.stdout:
        return None
    if result.returncode != 0 or result.output_truncated:
        raise RepoPublishFailure(
            "TRANSIENT_FAILURE",
            "fixed origin branch observation failed",
            retryable=True,
        )
    text = _text(result)
    lines = [line for line in text.splitlines() if line]
    if len(lines) != 1:
        raise RepoPublishFailure(
            "TRANSIENT_FAILURE",
            "fixed origin branch observation was not singular",
            retryable=True,
        )
    fields = lines[0].split()
    expected_ref = f"refs/heads/{branch}"
    if (
        len(fields) != 2
        or fields[1] != expected_ref
        or len(fields[0]) != 40
        or any(char not in "0123456789abcdef" for char in fields[0])
    ):
        raise RepoPublishFailure(
            "TRANSIENT_FAILURE",
            "fixed origin branch observation was malformed",
            retryable=True,
        )
    return fields[0]


def _observe_remote_head(
    repo,
    branch: str,
    deadline: float,
    *,
    after_push: bool,
) -> str:
    try:
        result = _run_git(
            repo,
            ["ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
            deadline=deadline,
        )
        head = _parse_remote_head(result, branch)
    except (RepoFastForwardFailure, RepoPublishFailure) as exc:
        if after_push:
            raise RepoPublishFailure(
                "PUBLICATION_AMBIGUOUS",
                "post-push remote state could not be established; re-observe before retry",
                retryable=True,
            ) from exc
        if isinstance(exc, RepoFastForwardFailure):
            raise _translate_failure(exc) from None
        raise

    if head is None:
        if after_push:
            raise RepoPublishFailure(
                "PUBLICATION_AMBIGUOUS",
                "post-push remote branch state is unavailable; re-observe before retry",
                retryable=True,
            )
        raise RepoPublishFailure(
            "REMOTE_HEAD_MISMATCH",
            "fixed origin branch must already exist",
        )
    return head


def _require_unchanged_local_state(
    repo,
    initial: _LocalState,
    deadline: float,
) -> None:
    try:
        current = _read_local_state(repo, deadline)
    except RepoFastForwardFailure as exc:
        if exc.code in {
            "DIRTY_WORKTREE",
            "OPERATION_IN_PROGRESS",
            "DETACHED_HEAD",
        }:
            raise RepoPublishFailure(
                "LOCAL_STATE_CHANGED",
                "local repository state changed before publication; re-observe before retry",
            ) from None
        raise _translate_failure(exc) from None
    if current != initial:
        raise RepoPublishFailure(
            "LOCAL_STATE_CHANGED",
            "local repository state changed before publication; re-observe before retry",
        )


def _push_deadline(deadline: float) -> float:
    latest = deadline - _POST_PUSH_RESERVE_SECONDS
    if latest <= time.monotonic():
        raise RepoPublishFailure(
            "DEADLINE_EXCEEDED",
            "repo_publish lacks bounded time for push and postcondition observation",
            retryable=True,
        )
    return latest
def _push_once(
    repo,
    branch: str,
    expected_remote_head: str,
    commit: str,
    deadline: float,
):
    return _run_git(
        repo,
        [
            "-c",
            "push.followTags=false",
            "push",
            "--no-verify",
            "--recurse-submodules=no",
            f"--force-with-lease=refs/heads/{branch}:{expected_remote_head}",
            "origin",
            f"{commit}:refs/heads/{branch}",
        ],
        deadline=_push_deadline(deadline),
        disable_hooks=True,
    )


def _publish_repository(
    cwd: str,
    branch: str,
    expected_remote_head: str,
    commit: str,
) -> RepoPublishResult:
    workspace_root = _workspace_root()
    validated_cwd = _validate_cwd(cwd, workspace_root)
    _validate_sha(expected_remote_head, "expected_remote_head")
    _validate_sha(commit, "commit")
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    repo = _require_repository_root(validated_cwd, deadline)
    _validate_branch(repo, branch, deadline)
    initial = _read_local_state(repo, deadline)
    _validate_bound_local_state(initial, branch, commit)
    _require_commit_exists(repo, commit, deadline)
    _require_direct_child(repo, expected_remote_head, commit, deadline)

    remote_before = _observe_remote_head(
        repo,
        branch,
        deadline,
        after_push=False,
    )
    if remote_before == commit:
        return RepoPublishResult(
            schema_version=1,
            status="already_published",
            repository_root=str(repo),
            branch=branch,
            remote="origin",
            upstream=f"origin/{branch}",
            expected_remote_head=expected_remote_head,
            commit=commit,
            head=initial.head,
            remote_head_before=remote_before,
            remote_head_after=remote_before,
            network_used=True,
            push_attempted=False,
            published=False,
            deadline_seconds=CALL_DEADLINE_SECONDS,
        )
    if remote_before != expected_remote_head:
        raise RepoPublishFailure(
            "REMOTE_HEAD_MISMATCH",
            "fixed origin branch does not match the expected remote commit",
        )

    remote_before = _observe_remote_head(
        repo,
        branch,
        deadline,
        after_push=False,
    )
    if remote_before == commit:
        return RepoPublishResult(
            schema_version=1,
            status="already_published",
            repository_root=str(repo),
            branch=branch,
            remote="origin",
            upstream=f"origin/{branch}",
            expected_remote_head=expected_remote_head,
            commit=commit,
            head=initial.head,
            remote_head_before=remote_before,
            remote_head_after=remote_before,
            network_used=True,
            push_attempted=False,
            published=False,
            deadline_seconds=CALL_DEADLINE_SECONDS,
        )
    if remote_before != expected_remote_head:
        raise RepoPublishFailure(
            "REMOTE_HEAD_MISMATCH",
            "fixed origin branch changed before publication",
        )

    _require_unchanged_local_state(repo, initial, deadline)
    _require_expected_tracking_head(
        repo,
        branch,
        expected_remote_head,
        deadline,
    )

    push_result = None
    push_error: RepoFastForwardFailure | None = None
    try:
        push_result = _push_once(
            repo,
            branch,
            expected_remote_head,
            commit,
            deadline,
        )
    except RepoFastForwardFailure as exc:
        push_error = exc

    remote_after = _observe_remote_head(
        repo,
        branch,
        deadline,
        after_push=True,
    )
    if remote_after == commit:
        return RepoPublishResult(
            schema_version=1,
            status="published",
            repository_root=str(repo),
            branch=branch,
            remote="origin",
            upstream=f"origin/{branch}",
            expected_remote_head=expected_remote_head,
            commit=commit,
            head=initial.head,
            remote_head_before=remote_before,
            remote_head_after=remote_after,
            network_used=True,
            push_attempted=True,
            published=True,
            deadline_seconds=CALL_DEADLINE_SECONDS,
        )
    if remote_after != expected_remote_head:
        raise RepoPublishFailure(
            "REMOTE_HEAD_MISMATCH",
            "fixed origin branch changed to an unexpected commit during publication",
        )
    if push_error is not None or (
        push_result is not None
        and (push_result.returncode != 0 or push_result.output_truncated)
    ):
        raise RepoPublishFailure(
            "PUSH_FAILED",
            "fixed-origin publication failed and the remote head remained unchanged",
            retryable=True,
        )
    raise RepoPublishFailure(
        "PUBLICATION_AMBIGUOUS",
        "push reported success but remote publication was not proven; re-observe before retry",
        retryable=True,
    )


def publish_repository(
    cwd: str,
    branch: str,
    expected_remote_head: str,
    commit: str,
) -> RepoPublishResult:
    try:
        return _publish_repository(
            cwd,
            branch,
            expected_remote_head,
            commit,
        )
    except RepoPublishFailure:
        raise
    except RepoFastForwardFailure as exc:
        raise _translate_failure(exc) from None
