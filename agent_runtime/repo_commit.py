from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import (
    CapabilityFailure,
    ReceiptV1Result,
    RepoCommitReceiptResult,
    RepoCommitResult,
)
from .errors import RuntimeValidationError
from .fs_safety import FsSafetyError, open_validated_cwd
from .repo_diff import diff_repository
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
MAX_MESSAGE_BYTES = 16 * 1024
MAX_STAGED_PATHS = 50
MAX_BLOB_BYTES = 1024 * 1024
MAX_AGGREGATE_STAGED_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_PATHS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "rebase-merge",
    "rebase-apply",
    "BISECT_START",
)

REPO_COMMIT_CONTRACT = ToolContract(
    name="repo_commit",
    tool_class=ToolClass.REPO,
    authority=Authority(
        workspace_bound=True,
        network=NetworkAuthority.NONE,
        mutation=MutationAuthority.BOUNDED,
    ),
    annotations=ToolAnnotations(
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
    ),
    preconditions={
        "cwd": "exact-nonbare-repository-root-inside-workspace",
        "branch": "attached-current-branch-with-origin-upstream",
        "expected_head_sha": "exact-current-head",
        "expected_diff_receipt": "exact-current-repo-diff-staged-receipt",
        "worktree": "no-unstaged-or-nonignored-untracked-state",
        "staged_candidate": "ordinary-utf8-regular-add-modify-delete-or-executable-mode-change",
        "git_identity": "AGENT_RUNTIME_GIT_NAME-and-AGENT_RUNTIME_GIT_EMAIL-only",
    },
    bounds={
        "message_utf8_bytes": MAX_MESSAGE_BYTES,
        "staged_paths": MAX_STAGED_PATHS,
        "per_blob_bytes": MAX_BLOB_BYTES,
        "aggregate_staged_blob_bytes": MAX_AGGREGATE_STAGED_BYTES,
        "deadline_milliseconds": 5000,
    },
    postconditions={
        "commit_creation": "write-tree-then-commit-tree-one-parent",
        "branch_mutation": "update-ref-new-expected-old-cas",
        "git_commit": False,
        "staging": False,
        "network_used": False,
        "post_commit_clean": True,
        "receipt_kind": "repo-commit",
    },
)


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class _RepoState:
    root: Path
    root_fd: int
    branch: str
    head: str
    upstream: str


@dataclass(frozen=True, slots=True)
class _Entry:
    mode: str
    sha: str


def _fail(
    code: ContractErrorCode,
    reason: str,
    message: str,
    *,
    retryable: bool = False,
) -> CapabilityFailure:
    return CapabilityFailure(code, reason, message, retryable=retryable)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _fail(
            ContractErrorCode.TIMEOUT,
            "DEADLINE_EXCEEDED",
            "repo_commit exceeded its 5-second call deadline",
            retryable=True,
        )
    return remaining


def _base_git_env() -> dict[str, str]:
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
            "GIT_CONFIG_NOSYSTEM": "1",
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
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        *args,
    ]


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    deadline: float,
    stdin: bytes | None = None,
    stdout_limit: int = MAX_METADATA_BYTES,
    identity: tuple[str, str] | None = None,
) -> _GitResult:
    env = _base_git_env()
    if identity is not None:
        name, email = identity
        env.update(
            {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
            }
        )
    try:
        completed = subprocess.run(
            _git_argv(args),
            cwd=str(cwd),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
            timeout=_remaining(deadline),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _fail(
            ContractErrorCode.TIMEOUT,
            "DEADLINE_EXCEEDED",
            "repo_commit exceeded its 5-second call deadline",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "fixed local Git executable could not be started",
            retryable=True,
        ) from exc
    if len(completed.stdout) > stdout_limit or len(completed.stderr) > MAX_STDERR_BYTES:
        raise _fail(
            ContractErrorCode.LIMIT_EXCEEDED,
            "GIT_METADATA_LIMIT",
            "bounded Git output exceeded its configured limit",
        )
    return _GitResult(completed.returncode, completed.stdout, completed.stderr)


def _text(result: _GitResult, reason: str) -> str:
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            reason,
            "local Git repository precondition failed",
        )
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned invalid UTF-8 metadata",
        ) from exc


def _validate_sha40(value: str) -> None:
    if type(value) is not str or _SHA40_RE.fullmatch(value) is None:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "HEAD_MISMATCH",
            "expected_head_sha must be exact lowercase 40-hex",
        )


def _validate_branch(repo: Path, branch: str, deadline: float) -> None:
    if (
        type(branch) is not str
        or not branch
        or len(branch) > 255
        or branch.startswith("-")
    ):
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_BRANCH",
            "branch must be a bounded legal Git branch name",
        )
    result = _run_git(repo, ["check-ref-format", "--branch", branch], deadline=deadline)
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_BRANCH",
            "branch must be a bounded legal Git branch name",
        )


def _operation_in_progress(repo: Path, deadline: float) -> bool:
    for name in _OPERATION_PATHS:
        path_result = _run_git(repo, ["rev-parse", "--git-path", name], deadline=deadline)
        path_text = _text(path_result, "OPERATION_IN_PROGRESS")
        path = Path(path_text)
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return True
    return False


def _open_repository(cwd: str, branch: str, expected_head_sha: str, deadline: float) -> _RepoState:
    _validate_sha40(expected_head_sha)
    try:
        root, root_fd = open_validated_cwd(cwd)
    except RuntimeValidationError as exc:
        if "outside AGENT_RUNTIME_WORKSPACE_ROOT" in str(exc):
            raise _fail(
                ContractErrorCode.OUTSIDE_WORKSPACE,
                "NOT_REPOSITORY_ROOT",
                "cwd resolves outside the configured workspace",
            ) from None
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "NOT_REPOSITORY_ROOT",
            "cwd must identify an existing absolute workspace directory",
        ) from None
    except FsSafetyError as exc:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_REPOSITORY_ROOT",
            "cwd could not be safely opened",
        ) from exc

    try:
        top = _text(
            _run_git(root, ["rev-parse", "--show-toplevel"], deadline=deadline),
            "NOT_REPOSITORY_ROOT",
        )
        try:
            resolved_top = Path(top).resolve(strict=True)
        except OSError as exc:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "NOT_REPOSITORY_ROOT",
                "repository root could not be resolved",
            ) from exc
        if resolved_top != root:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "NOT_REPOSITORY_ROOT",
                "cwd must be exactly the repository root",
            )
        bare = _text(
            _run_git(root, ["rev-parse", "--is-bare-repository"], deadline=deadline),
            "BARE_REPOSITORY",
        )
        if bare != "false":
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "BARE_REPOSITORY",
                "bare repositories are not supported",
            )
        _validate_branch(root, branch, deadline)
        branch_result = _run_git(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            deadline=deadline,
        )
        if branch_result.returncode == 1:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "DETACHED_HEAD",
                "repository HEAD must be attached",
            )
        current_branch = _text(branch_result, "DETACHED_HEAD")
        if current_branch != branch:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "BRANCH_MISMATCH",
                "current branch does not match requested branch",
            )
        head = _text(
            _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"], deadline=deadline),
            "HEAD_MISMATCH",
        )
        if head != expected_head_sha:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "HEAD_MISMATCH",
                "current HEAD does not match expected_head_sha",
            )
        upstream_result = _run_git(
            root,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            deadline=deadline,
        )
        upstream = _text(upstream_result, "UPSTREAM_MISMATCH")
        if upstream != f"origin/{branch}":
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "UPSTREAM_MISMATCH",
                "current branch upstream must equal origin/<branch>",
            )
        if _operation_in_progress(root, deadline):
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "OPERATION_IN_PROGRESS",
                "repository has an in-progress Git operation",
            )
        return _RepoState(root, root_fd, current_branch, head, upstream)
    except Exception:
        os.close(root_fd)
        raise


def _validate_message(message: str) -> bytes:
    if type(message) is not str:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_COMMIT_MESSAGE",
            "message must be a strict UTF-8 string",
        )
    try:
        raw = message.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_COMMIT_MESSAGE",
            "message must be valid UTF-8",
        ) from exc
    if not raw or len(raw) > MAX_MESSAGE_BYTES or b"\x00" in raw:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_COMMIT_MESSAGE",
            "message must contain 1..16384 UTF-8 bytes and no NUL",
        )
    return raw


def _validate_diff_receipt(receipt: ReceiptV1Result) -> str:
    if not isinstance(receipt, ReceiptV1Result):
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_DIFF_RECEIPT",
            "expected_diff_receipt must be a closed repo-diff ReceiptV1",
        )
    if receipt.schema_version != 1 or receipt.kind != "repo-diff":
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_DIFF_RECEIPT",
            "expected_diff_receipt must identify repo-diff schema version 1",
        )
    if _SHA256_RE.fullmatch(receipt.digest) is None:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_DIFF_RECEIPT",
            "expected_diff_receipt digest must be lowercase SHA-256 hex",
        )
    return receipt.digest


def _validate_identity() -> tuple[str, str]:
    values: list[str] = []
    for variable in ("AGENT_RUNTIME_GIT_NAME", "AGENT_RUNTIME_GIT_EMAIL"):
        value = os.environ.get(variable)
        if type(value) is not str or not value:
            raise _fail(
                ContractErrorCode.UNAVAILABLE,
                "GIT_IDENTITY_UNAVAILABLE",
                "Runtime Git identity is unavailable",
            )
        try:
            raw = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise _fail(
                ContractErrorCode.UNAVAILABLE,
                "GIT_IDENTITY_UNAVAILABLE",
                "Runtime Git identity is invalid",
            ) from exc
        if len(raw) > 256 or any(ch in value for ch in ("\x00", "\r", "\n")):
            raise _fail(
                ContractErrorCode.UNAVAILABLE,
                "GIT_IDENTITY_UNAVAILABLE",
                "Runtime Git identity is invalid",
            )
        values.append(value)
    return values[0], values[1]


def _require_bound_state(state: _RepoState, expected_head_sha: str, deadline: float) -> None:
    if _operation_in_progress(state.root, deadline):
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "OPERATION_IN_PROGRESS",
            "repository has an in-progress Git operation",
        )
    branch_result = _run_git(
        state.root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        deadline=deadline,
    )
    if branch_result.returncode != 0:
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "HEAD_CHANGED",
            "repository branch state changed",
        )
    branch = _text(branch_result, "HEAD_CHANGED")
    if branch != state.branch:
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "HEAD_CHANGED",
            "repository branch state changed",
        )
    head = _text(
        _run_git(state.root, ["rev-parse", "--verify", "HEAD^{commit}"], deadline=deadline),
        "HEAD_CHANGED",
    )
    if head != expected_head_sha:
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "HEAD_CHANGED",
            "HEAD changed before commit branch update",
        )
    conflicts = _run_git(state.root, ["ls-files", "-u", "-z"], deadline=deadline)
    if conflicts.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect index conflicts",
            retryable=True,
        )
    if conflicts.stdout:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "CONFLICTS_PRESENT",
            "index contains conflicted entries",
        )


def _require_clean_worktree(repo: Path, deadline: float) -> None:
    worktree = diff_repository(str(repo), "worktree")
    if worktree.full_diff_bytes != 0:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSTAGED_CHANGES_PRESENT",
            "repo_commit requires no tracked unstaged changes",
        )
    untracked = _run_git(
        repo,
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
        deadline=deadline,
    )
    if untracked.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect untracked files",
            retryable=True,
        )
    if untracked.stdout:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNTRACKED_CHANGES_PRESENT",
            "repo_commit requires no non-ignored untracked files",
        )


def _staged_diff(
    state: _RepoState,
    expected_digest: str,
) -> ReceiptV1Result:
    staged = diff_repository(str(state.root), "staged")
    if staged.full_diff_bytes == 0:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "NO_STAGED_CHANGES",
            "repo_commit requires non-empty staged changes",
        )
    if staged.diff_receipt.digest != expected_digest:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "DIFF_RECEIPT_MISMATCH",
            "current staged repo-diff receipt does not match expected_diff_receipt",
        )
    return ReceiptV1Result(
        schema_version=1,
        kind="repo-diff",
        digest=staged.diff_receipt.digest,
    )


def _staged_paths(repo: Path, deadline: float) -> list[str]:
    result = _run_git(
        repo,
        ["diff", "--cached", "--name-only", "-z", "--no-renames", "--"],
        deadline=deadline,
    )
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect staged paths",
            retryable=True,
        )
    paths: list[str] = []
    for raw in result.stdout.split(b"\x00"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "UNSUPPORTED_STAGED_TYPE",
                "staged path is not valid UTF-8",
            ) from exc
        paths.append(path)
    if not paths:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "NO_STAGED_CHANGES",
            "repo_commit requires non-empty staged changes",
        )
    if len(paths) > MAX_STAGED_PATHS:
        raise _fail(
            ContractErrorCode.LIMIT_EXCEEDED,
            "STAGED_PATH_LIMIT",
            "staged candidate exceeds 50 paths",
        )
    if len(set(paths)) != len(paths):
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSUPPORTED_STAGED_TYPE",
            "staged candidate contains ambiguous duplicate paths",
        )
    return paths


def _head_entry(repo: Path, path: str, deadline: float) -> _Entry | None:
    result = _run_git(repo, ["ls-tree", "-z", "HEAD", "--", path], deadline=deadline)
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect HEAD entry",
            retryable=True,
        )
    records = [record for record in result.stdout.split(b"\x00") if record]
    if not records:
        return None
    if len(records) != 1:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSUPPORTED_STAGED_TYPE",
            "staged path maps to an unsupported HEAD entry",
        )
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode_raw, type_raw, sha_raw = metadata.split(b" ", 2)
        mode = mode_raw.decode("ascii", errors="strict")
        object_type = type_raw.decode("ascii", errors="strict")
        sha = sha_raw.decode("ascii", errors="strict")
        observed_path = raw_path.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned malformed tree metadata",
        ) from exc
    if observed_path != path or object_type != "blob" or mode not in {"100644", "100755"}:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSUPPORTED_STAGED_TYPE",
            "only ordinary regular blob modes are supported",
        )
    if _SHA40_RE.fullmatch(sha) is None:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned invalid blob identifier",
        )
    return _Entry(mode, sha)


def _index_entry(repo: Path, path: str, deadline: float) -> _Entry | None:
    result = _run_git(repo, ["ls-files", "--stage", "-z", "--", path], deadline=deadline)
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect index entry",
            retryable=True,
        )
    records = [record for record in result.stdout.split(b"\x00") if record]
    if not records:
        return None
    if len(records) != 1:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "CONFLICTS_PRESENT",
            "staged path has multiple index stages",
        )
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode_raw, sha_raw, stage_raw = metadata.split(b" ", 2)
        mode = mode_raw.decode("ascii", errors="strict")
        sha = sha_raw.decode("ascii", errors="strict")
        stage = stage_raw.decode("ascii", errors="strict")
        observed_path = raw_path.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned malformed index metadata",
        ) from exc
    if observed_path != path or stage != "0" or mode not in {"100644", "100755"}:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSUPPORTED_STAGED_TYPE",
            "only stage-zero ordinary regular blob modes are supported",
        )
    if _SHA40_RE.fullmatch(sha) is None:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned invalid index blob identifier",
        )
    return _Entry(mode, sha)


def _blob_bytes(repo: Path, entry: _Entry, deadline: float) -> bytes:
    size_result = _run_git(repo, ["cat-file", "-s", entry.sha], deadline=deadline)
    size_text = _text(size_result, "UNSUPPORTED_STAGED_TYPE")
    try:
        size = int(size_text)
    except ValueError as exc:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned invalid blob size",
        ) from exc
    if size > MAX_BLOB_BYTES:
        raise _fail(
            ContractErrorCode.LIMIT_EXCEEDED,
            "STAGED_BYTES_LIMIT",
            "staged blob exceeds 1 MiB",
        )
    result = _run_git(
        repo,
        ["cat-file", "blob", entry.sha],
        deadline=deadline,
        stdout_limit=MAX_BLOB_BYTES,
    )
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to read staged Git blob",
            retryable=True,
        )
    raw = result.stdout
    if b"\x00" in raw:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "BINARY_STAGED_CONTENT",
            "staged candidate contains binary NUL content",
        )
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "INVALID_UTF8_STAGED_CONTENT",
            "staged candidate contains invalid UTF-8 content",
        ) from exc
    return raw


def _validate_staged_candidate(repo: Path, deadline: float) -> None:
    paths = _staged_paths(repo, deadline)
    aggregate = 0
    for path in paths:
        head = _head_entry(repo, path, deadline)
        index = _index_entry(repo, path, deadline)
        if head is None and index is None:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "UNSUPPORTED_STAGED_TYPE",
                "staged path has no supported before or after blob",
            )
        if index is not None:
            raw = _blob_bytes(repo, index, deadline)
        else:
            assert head is not None
            raw = _blob_bytes(repo, head, deadline)
        aggregate += len(raw)
        if aggregate > MAX_AGGREGATE_STAGED_BYTES:
            raise _fail(
                ContractErrorCode.LIMIT_EXCEEDED,
                "STAGED_BYTES_LIMIT",
                "aggregate staged blob content exceeds 16 MiB",
            )


def _write_tree(repo: Path, deadline: float) -> str:
    result = _run_git(repo, ["write-tree"], deadline=deadline, stdout_limit=128)
    tree = _text(result, "TREE_CAPTURE_FAILED")
    if _SHA40_RE.fullmatch(tree) is None:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "TREE_CAPTURE_FAILED",
            "write-tree returned an invalid tree identifier",
        )
    return tree


def _recheck_before_commit(
    state: _RepoState,
    expected_head_sha: str,
    expected_digest: str,
    expected_tree: str,
    deadline: float,
) -> None:
    _require_bound_state(state, expected_head_sha, deadline)
    _require_clean_worktree(state.root, deadline)
    _staged_diff(state, expected_digest)
    tree = _write_tree(state.root, deadline)
    if tree != expected_tree:
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "INDEX_CHANGED",
            "index tree changed during repo_commit",
        )


def commit_repository(
    cwd: str,
    branch: str,
    expected_head_sha: str,
    expected_diff_receipt: ReceiptV1Result,
    message: str,
) -> RepoCommitResult:
    """Commit exactly the authorized staged state using Git plumbing and update-ref CAS."""

    message_bytes = _validate_message(message)
    expected_digest = _validate_diff_receipt(expected_diff_receipt)
    identity = _validate_identity()
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    state = _open_repository(cwd, branch, expected_head_sha, deadline)
    try:
        _require_bound_state(state, expected_head_sha, deadline)
        _require_clean_worktree(state.root, deadline)
        diff_receipt = _staged_diff(state, expected_digest)
        _validate_staged_candidate(state.root, deadline)

        tree_sha = _write_tree(state.root, deadline)
        _recheck_before_commit(
            state,
            expected_head_sha,
            expected_digest,
            tree_sha,
            deadline,
        )

        commit_result = _run_git(
            state.root,
            ["commit-tree", tree_sha, "-p", expected_head_sha],
            deadline=deadline,
            stdin=message_bytes,
            stdout_limit=128,
            identity=identity,
        )
        commit_sha = _text(commit_result, "COMMIT_OBJECT_FAILED")
        if _SHA40_RE.fullmatch(commit_sha) is None:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "COMMIT_OBJECT_FAILED",
                "commit-tree returned an invalid commit identifier",
            )

        _require_bound_state(state, expected_head_sha, deadline)
        current_tree = _write_tree(state.root, deadline)
        if current_tree != tree_sha:
            raise _fail(
                ContractErrorCode.STATE_CHANGED,
                "INDEX_CHANGED",
                "index tree changed before branch compare-and-swap",
            )

        updated = _run_git(
            state.root,
            ["update-ref", f"refs/heads/{branch}", commit_sha, expected_head_sha],
            deadline=deadline,
        )
        if updated.returncode != 0:
            current_head_result = _run_git(
                state.root,
                ["rev-parse", "--verify", "HEAD^{commit}"],
                deadline=deadline,
            )
            current_head = (
                _text(current_head_result, "REF_UPDATE_FAILED")
                if current_head_result.returncode == 0
                else ""
            )
            if current_head != expected_head_sha:
                raise _fail(
                    ContractErrorCode.STATE_CHANGED,
                    "HEAD_CHANGED",
                    "branch compare-and-swap failed because HEAD changed",
                )
            raise _fail(
                ContractErrorCode.CONFLICT,
                "REF_UPDATE_FAILED",
                "branch compare-and-swap update failed",
            )

        final_head = _text(
            _run_git(state.root, ["rev-parse", "--verify", "HEAD^{commit}"], deadline=deadline),
            "POST_COMMIT_STATE_INVALID",
        )
        if final_head != commit_sha:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "POST_COMMIT_STATE_INVALID",
                "HEAD does not equal committed object after update-ref",
            )
        final_branch = _text(
            _run_git(state.root, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline=deadline),
            "POST_COMMIT_STATE_INVALID",
        )
        if final_branch != branch:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "POST_COMMIT_STATE_INVALID",
                "current branch changed after commit",
            )
        if _write_tree(state.root, deadline) != tree_sha:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "POST_COMMIT_STATE_INVALID",
                "index tree no longer equals committed tree",
            )
        _require_clean_worktree(state.root, deadline)
        staged_after = diff_repository(str(state.root), "staged")
        if staged_after.full_diff_bytes != 0:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "POST_COMMIT_STATE_INVALID",
                "staged changes remain after commit",
            )

        commit_object = _run_git(
            state.root,
            ["cat-file", "commit", commit_sha],
            deadline=deadline,
            stdout_limit=MAX_METADATA_BYTES,
        )
        if commit_object.returncode != 0:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "POST_COMMIT_STATE_INVALID",
                "committed object could not be re-read",
            )
        receipt = make_receipt_v1(
            kind="repo-commit",
            subject={
                "repository_root": str(state.root),
                "branch": branch,
                "commit_sha": commit_sha,
            },
            semantic_parameters={
                "parent_sha": expected_head_sha,
                "expected_diff_receipt_digest": expected_digest,
                "commit_message_sha256": hashlib.sha256(message_bytes).hexdigest(),
            },
            observed_state_bytes=commit_object.stdout,
        )
        return RepoCommitResult(
            schema_version=1,
            branch=branch,
            parent_sha=expected_head_sha,
            tree_sha=tree_sha,
            commit_sha=commit_sha,
            diff_receipt=diff_receipt,
            commit_receipt=RepoCommitReceiptResult(
                schema_version=1,
                kind="repo-commit",
                digest=receipt.digest,
            ),
            network_used=False,
            post_commit_clean=True,
        )
    finally:
        os.close(state.root_fd)
