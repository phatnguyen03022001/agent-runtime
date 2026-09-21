from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import (
    CapabilityFailure,
    ReceiptV1Result,
    RepoStageItem,
    RepoStagePathResult,
    RepoStageResult,
)
from .errors import RuntimeValidationError
from .fs_safety import (
    FsSafetyError,
    file_identity,
    open_parent_at,
    open_regular_from_parent,
    open_validated_cwd,
    split_descendant,
)
from .repo_diff import diff_repository
from .tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

GIT_EXECUTABLE = "/usr/bin/git"
CALL_DEADLINE_SECONDS = 5.0
MAX_ITEMS = 50
MAX_PATH_CHARS = 4096
MAX_FILE_BYTES = 1024 * 1024
MAX_AGGREGATE_BYTES = 16 * 1024 * 1024
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

REPO_STAGE_CONTRACT = ToolContract(
    name="repo_stage",
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
        "staged_diff": "empty",
        "candidate": "complete-explicit-current-worktree-and-nonignored-untracked-set",
        "items": {
            "max": MAX_ITEMS,
            "types": ["regular-utf8-present", "tracked-regular-delete"],
            "expected_sha": "mandatory-for-present-forbidden-for-delete",
        },
    },
    bounds={
        "items": MAX_ITEMS,
        "path_chars": MAX_PATH_CHARS,
        "per_file_bytes": MAX_FILE_BYTES,
        "aggregate_present_bytes": MAX_AGGREGATE_BYTES,
        "metadata_output_bytes": MAX_METADATA_BYTES,
        "deadline_milliseconds": 5000,
    },
    postconditions={
        "head_unchanged": True,
        "index_mutation": "one-update-index-index-info-invocation",
        "git_add": False,
        "staged_paths": "exact-candidate",
        "staged_receipt_kind": "repo-diff",
        "post_stage_clean": True,
        "network_used": False,
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
class _HeadEntry:
    mode: str
    object_type: str
    sha: str


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    path: str
    operation: str
    expected_sha256: str | None
    raw: bytes | None
    sha256: str | None
    mode: str | None
    identity: tuple[int, int] | None


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
            "repo_stage exceeded its 5-second call deadline",
            retryable=True,
        )
    return remaining


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
) -> _GitResult:
    try:
        completed = subprocess.run(
            _git_argv(args),
            cwd=str(cwd),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
            shell=False,
            timeout=_remaining(deadline),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _fail(
            ContractErrorCode.TIMEOUT,
            "DEADLINE_EXCEEDED",
            "repo_stage exceeded its 5-second call deadline",
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
            "bounded Git metadata exceeded its output limit",
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


def _validate_sha40(value: str, field: str) -> None:
    if type(value) is not str or _SHA40_RE.fullmatch(value) is None:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "HEAD_MISMATCH" if field == "expected_head_sha" else "INVALID_BRANCH",
            f"{field} must be exact lowercase 40-hex",
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
    checked = _run_git(repo, ["check-ref-format", "--branch", branch], deadline=deadline)
    if checked.returncode != 0:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_BRANCH",
            "branch must be a bounded legal Git branch name",
        )


def _operation_in_progress(repo: Path, deadline: float) -> bool:
    for name in _OPERATION_PATHS:
        result = _run_git(repo, ["rev-parse", "--git-path", name], deadline=deadline)
        path_text = _text(result, "OPERATION_IN_PROGRESS")
        path = Path(path_text)
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return True
    return False


def _open_repository(cwd: str, branch: str, expected_head_sha: str, deadline: float) -> _RepoState:
    _validate_sha40(expected_head_sha, "expected_head_sha")
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
        current_branch_result = _run_git(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            deadline=deadline,
        )
        if current_branch_result.returncode == 1:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "DETACHED_HEAD",
                "repository HEAD must be attached",
            )
        current_branch = _text(current_branch_result, "DETACHED_HEAD")
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


def _validate_path(path: str) -> tuple[str, ...]:
    try:
        components = split_descendant(path, allow_dot=False)
    except ValueError as exc:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "path must be a normalized repository-relative descendant",
        ) from exc
    if components[0].lower() == ".git":
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "paths inside .git are not allowed",
        )
    return components


def _validate_items(items: list[RepoStageItem]) -> list[RepoStageItem]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_ITEMS",
            "items must contain between 1 and 50 entries",
        )
    seen: set[str] = set()
    aggregate_paths = 0
    for item in items:
        if not isinstance(item, RepoStageItem):
            raise _fail(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_ITEMS",
                "each item must be a closed repo_stage item",
            )
        _validate_path(item.path)
        aggregate_paths += len(item.path)
        if aggregate_paths > MAX_ITEMS * MAX_PATH_CHARS:
            raise _fail(
                ContractErrorCode.LIMIT_EXCEEDED,
                "INVALID_ITEMS",
                "aggregate item paths exceed the configured bound",
            )
        if item.path in seen:
            raise _fail(
                ContractErrorCode.INVALID_ARGUMENT,
                "DUPLICATE_PATH",
                "item paths must be unique",
            )
        seen.add(item.path)
        if item.operation == "present":
            if item.expected_sha256 is None:
                raise _fail(
                    ContractErrorCode.INVALID_ARGUMENT,
                    "EXPECTED_SHA_REQUIRED",
                    "expected_sha256 is required for present items",
                )
            if _SHA256_RE.fullmatch(item.expected_sha256) is None:
                raise _fail(
                    ContractErrorCode.INVALID_ARGUMENT,
                    "EXPECTED_SHA_REQUIRED",
                    "expected_sha256 must be lowercase SHA-256 hex",
                )
        elif item.operation == "delete":
            if item.expected_sha256 is not None:
                raise _fail(
                    ContractErrorCode.INVALID_ARGUMENT,
                    "EXPECTED_SHA_FORBIDDEN",
                    "expected_sha256 must be null for delete items",
                )
        else:
            raise _fail(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_ITEMS",
                "operation must be present or delete",
            )
    return items


def _status_paths(repo: Path, deadline: float) -> tuple[set[str], bool, bool]:
    result = _run_git(
        repo,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
            "--no-renames",
        ],
        deadline=deadline,
    )
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect repository candidate state",
            retryable=True,
        )
    paths: set[str] = set()
    staged = False
    conflicts = False
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        if len(record) < 4:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "INVALID_GIT_METADATA",
                "local Git returned malformed status metadata",
            )
        x = chr(record[0])
        y = chr(record[1])
        if record[2:3] != b" ":
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "INVALID_GIT_METADATA",
                "local Git returned malformed status metadata",
            )
        try:
            path = record[3:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "INVALID_PATH",
                "repository contains a non-UTF-8 candidate path",
            ) from exc
        _validate_path(path)
        paths.add(path)
        if x not in {" ", "?"}:
            staged = True
        if "U" in (x, y) or (x + y) in {"AA", "DD"}:
            conflicts = True
    return paths, staged, conflicts


def _require_candidate_state(
    state: _RepoState,
    selected_paths: set[str],
    expected_head_sha: str,
    deadline: float,
) -> None:
    current_branch = _text(
        _run_git(state.root, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline=deadline),
        "DETACHED_HEAD",
    )
    if current_branch != state.branch:
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "current branch changed before index mutation",
        )
    current_head = _text(
        _run_git(state.root, ["rev-parse", "--verify", "HEAD^{commit}"], deadline=deadline),
        "HEAD_MISMATCH",
    )
    if current_head != expected_head_sha:
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "HEAD changed before index mutation",
        )
    upstream = _text(
        _run_git(
            state.root,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            deadline=deadline,
        ),
        "UPSTREAM_MISMATCH",
    )
    if upstream != f"origin/{state.branch}":
        raise _fail(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "branch upstream changed before index mutation",
        )
    if _operation_in_progress(state.root, deadline):
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "OPERATION_IN_PROGRESS",
            "repository has an in-progress Git operation",
        )
    paths, staged, conflicts = _status_paths(state.root, deadline)
    if conflicts:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "CONFLICTS_PRESENT",
            "repository has conflicted paths",
        )
    staged_diff = diff_repository(str(state.root), "staged")
    if staged_diff.full_diff_bytes != 0:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "STAGED_CHANGES_PRESENT",
            "repo_stage requires an initially empty staged diff",
        )
    if staged:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "STAGED_CHANGES_PRESENT",
            "repo_stage requires an initially empty staged diff",
        )
    if paths != selected_paths:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSELECTED_CHANGES_PRESENT",
            "selected items must equal the complete current candidate path set",
        )


def _head_entry(repo: Path, path: str, deadline: float) -> _HeadEntry | None:
    result = _run_git(repo, ["ls-tree", "-z", "HEAD", "--", path], deadline=deadline)
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to inspect HEAD entry",
            retryable=True,
        )
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\x00") if record]
    if len(records) != 1:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSUPPORTED_GIT_MODE",
            "candidate path does not map to one ordinary HEAD entry",
        )
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode_raw, type_raw, sha_raw = metadata.split(b" ", 2)
        observed_path = raw_path.decode("utf-8", errors="strict")
        mode = mode_raw.decode("ascii", errors="strict")
        object_type = type_raw.decode("ascii", errors="strict")
        sha = sha_raw.decode("ascii", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned malformed tree metadata",
        ) from exc
    if observed_path != path or _SHA40_RE.fullmatch(sha) is None:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned unexpected tree metadata",
        )
    return _HeadEntry(mode, object_type, sha)


def _blob_bytes(repo: Path, entry: _HeadEntry, deadline: float) -> bytes:
    if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "UNSUPPORTED_GIT_MODE",
            "only ordinary regular Git blob modes are supported",
        )
    size_result = _run_git(repo, ["cat-file", "-s", entry.sha], deadline=deadline)
    size_text = _text(size_result, "UNSUPPORTED_GIT_MODE")
    try:
        size = int(size_text)
    except ValueError as exc:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INVALID_GIT_METADATA",
            "local Git returned invalid blob size",
        ) from exc
    if size > MAX_FILE_BYTES:
        raise _fail(
            ContractErrorCode.LIMIT_EXCEEDED,
            "INPUT_FILE_LIMIT",
            "baseline Git blob exceeds 1 MiB",
        )
    result = _run_git(
        repo,
        ["cat-file", "blob", entry.sha],
        deadline=deadline,
        stdout_limit=MAX_FILE_BYTES,
    )
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.UNAVAILABLE,
            "GIT_UNAVAILABLE",
            "unable to read baseline Git blob",
            retryable=True,
        )
    _require_text(result.stdout, baseline=True)
    return result.stdout


def _require_text(raw: bytes, *, baseline: bool = False) -> None:
    if b"\x00" in raw:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "BINARY_CONTENT",
            "candidate contains NUL bytes",
        )
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "INVALID_UTF8",
            "candidate is not valid UTF-8 text",
        ) from exc


def _read_present(root_fd: int, path: str) -> tuple[bytes, str, tuple[int, int]]:
    components = _validate_path(path)
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd, final = open_parent_at(root_fd, components)
        file_fd, observed = open_regular_from_parent(parent_fd, final)
        if observed.st_size > MAX_FILE_BYTES:
            raise _fail(
                ContractErrorCode.LIMIT_EXCEEDED,
                "INPUT_FILE_LIMIT",
                "candidate file exceeds 1 MiB",
            )
        output = bytearray()
        while True:
            chunk = os.read(file_fd, min(64 * 1024, MAX_FILE_BYTES + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_FILE_BYTES:
                raise _fail(
                    ContractErrorCode.LIMIT_EXCEEDED,
                    "INPUT_FILE_LIMIT",
                    "candidate file exceeds 1 MiB",
                )
        raw = bytes(output)
        _require_text(raw)
        mode = "100755" if stat.S_IMODE(observed.st_mode) & 0o111 else "100644"
        return raw, mode, file_identity(observed)
    except FsSafetyError as exc:
        mapping = {
            "NOT_FOUND": ("TARGET_NOT_FOUND", ContractErrorCode.NOT_FOUND),
            "SYMLINK_DISALLOWED": ("SYMLINK_DISALLOWED", ContractErrorCode.PRECONDITION_FAILED),
            "NOT_REGULAR_FILE": ("NOT_REGULAR_FILE", ContractErrorCode.PRECONDITION_FAILED),
            "NOT_DIRECTORY": ("NOT_REGULAR_FILE", ContractErrorCode.PRECONDITION_FAILED),
            "ACCESS_DENIED": ("NOT_REGULAR_FILE", ContractErrorCode.PERMISSION_DENIED),
        }
        reason, code = mapping.get(exc.reason, ("NOT_REGULAR_FILE", ContractErrorCode.UNAVAILABLE))
        raise _fail(code, reason, "candidate path is not a supported regular file") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _require_absent(root_fd: int, path: str) -> None:
    components = _validate_path(path)
    current_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            try:
                observed = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(observed.st_mode):
                raise _fail(
                    ContractErrorCode.PRECONDITION_FAILED,
                    "SYMLINK_DISALLOWED",
                    "delete path may not traverse a symlink",
                )
            if not stat.S_ISDIR(observed.st_mode):
                raise _fail(
                    ContractErrorCode.PRECONDITION_FAILED,
                    "NOT_REGULAR_FILE",
                    "delete path parent is not a directory",
                )
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _fail(
                    ContractErrorCode.PRECONDITION_FAILED,
                    "SYMLINK_DISALLOWED",
                    "delete path parent could not be safely resolved",
                ) from exc
            os.close(current_fd)
            current_fd = next_fd

        final = components[-1]
        try:
            observed = os.stat(final, dir_fd=current_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(observed.st_mode):
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "SYMLINK_DISALLOWED",
                "delete target must be absent and may not be a symlink",
            )
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "TARGET_ALREADY_ABSENT",
            "delete operation requires the worktree path to be absent",
        )
    finally:
        os.close(current_fd)


def _is_ignored(repo: Path, path: str, deadline: float) -> bool:
    try:
        stdin = path.encode("utf-8", errors="strict") + b"\x00"
    except UnicodeEncodeError as exc:
        raise _fail(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "candidate path must be valid UTF-8",
        ) from exc
    result = _run_git(
        repo,
        ["check-ignore", "--stdin", "-z"],
        deadline=deadline,
        stdin=stdin,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise _fail(
        ContractErrorCode.UNAVAILABLE,
        "GIT_UNAVAILABLE",
        "unable to inspect Git ignore state",
        retryable=True,
    )


def _inspect_item(
    state: _RepoState,
    item: RepoStageItem,
    deadline: float,
) -> _PreparedItem:
    head = _head_entry(state.root, item.path, deadline)
    if head is not None:
        _blob_bytes(state.root, head, deadline)

    if item.operation == "present":
        raw, mode, identity = _read_present(state.root_fd, item.path)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != item.expected_sha256:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "EXPECTED_SHA_MISMATCH",
                "candidate SHA-256 does not match expected_sha256",
            )
        if head is None and _is_ignored(state.root, item.path, deadline):
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "IGNORED_PATH",
                "ignored untracked paths cannot be staged",
            )
        return _PreparedItem(
            path=item.path,
            operation=item.operation,
            expected_sha256=item.expected_sha256,
            raw=raw,
            sha256=digest,
            mode=mode,
            identity=identity,
        )

    _require_absent(state.root_fd, item.path)
    if head is None:
        raise _fail(
            ContractErrorCode.PRECONDITION_FAILED,
            "TARGET_ALREADY_ABSENT",
            "delete item must refer to a tracked HEAD path",
        )
    return _PreparedItem(
        path=item.path,
        operation=item.operation,
        expected_sha256=None,
        raw=None,
        sha256=None,
        mode=None,
        identity=None,
    )


def _prepare_all(
    state: _RepoState,
    items: list[RepoStageItem],
    deadline: float,
) -> list[_PreparedItem]:
    prepared: list[_PreparedItem] = []
    aggregate = 0
    for item in items:
        current = _inspect_item(state, item, deadline)
        if current.raw is not None:
            aggregate += len(current.raw)
            if aggregate > MAX_AGGREGATE_BYTES:
                raise _fail(
                    ContractErrorCode.LIMIT_EXCEEDED,
                    "AGGREGATE_FILE_LIMIT",
                    "aggregate present file bytes exceed 16 MiB",
                )
        prepared.append(current)
    return prepared


def _same_prepared(first: list[_PreparedItem], second: list[_PreparedItem]) -> bool:
    if len(first) != len(second):
        return False
    for left, right in zip(first, second):
        if (
            left.path != right.path
            or left.operation != right.operation
            or left.expected_sha256 != right.expected_sha256
            or left.sha256 != right.sha256
            or left.mode != right.mode
            or left.identity != right.identity
            or left.raw != right.raw
        ):
            return False
    return True


def _write_blob(repo: Path, raw: bytes, deadline: float) -> str:
    result = _run_git(
        repo,
        ["hash-object", "-w", "--stdin"],
        deadline=deadline,
        stdin=raw,
        stdout_limit=128,
    )
    sha = _text(result, "INDEX_UPDATE_FAILED")
    if _SHA40_RE.fullmatch(sha) is None:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INDEX_UPDATE_FAILED",
            "hash-object returned an invalid object identifier",
        )
    return sha


def _index_info_payload(
    prepared: list[_PreparedItem],
    blob_shas: dict[str, str],
) -> bytes:
    zero = "0" * 40
    chunks: list[bytes] = []
    for item in prepared:
        if item.operation == "delete":
            prefix = f"0 {zero}\t".encode("ascii")
        else:
            assert item.mode is not None
            prefix = f"{item.mode} {blob_shas[item.path]}\t".encode("ascii")
        chunks.append(prefix + item.path.encode("utf-8", errors="strict") + b"\x00")
    return b"".join(chunks)


def _staged_paths(repo: Path, deadline: float) -> set[str]:
    result = _run_git(
        repo,
        ["diff", "--cached", "--name-only", "-z", "--no-renames", "--"],
        deadline=deadline,
    )
    if result.returncode != 0:
        raise _fail(
            ContractErrorCode.INTERNAL_ERROR,
            "INDEX_UPDATE_FAILED",
            "unable to inspect staged path set",
        )
    paths: set[str] = set()
    for raw in result.stdout.split(b"\x00"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "INVALID_GIT_METADATA",
                "staged path is not valid UTF-8",
            ) from exc
        paths.add(path)
    return paths


def _post_stage_clean(repo: Path, deadline: float) -> bool:
    result = _run_git(
        repo,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
            "--no-renames",
        ],
        deadline=deadline,
    )
    if result.returncode != 0:
        return False
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        if len(record) < 3:
            return False
        x = chr(record[0])
        y = chr(record[1])
        if x == "?" or y != " ":
            return False
    return True


def stage_repository(
    cwd: str,
    branch: str,
    expected_head_sha: str,
    items: list[RepoStageItem],
) -> RepoStageResult:
    """Stage exactly one complete explicit regular-text candidate in one index transaction."""

    validated_items = _validate_items(items)
    selected_paths = {item.path for item in validated_items}
    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    state = _open_repository(cwd, branch, expected_head_sha, deadline)
    try:
        prepared = _prepare_all(state, validated_items, deadline)
        _require_candidate_state(state, selected_paths, expected_head_sha, deadline)

        _require_candidate_state(state, selected_paths, expected_head_sha, deadline)
        rechecked = _prepare_all(state, validated_items, deadline)
        if not _same_prepared(prepared, rechecked):
            raise _fail(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "candidate state changed before index mutation",
            )

        blob_shas: dict[str, str] = {}
        for item in rechecked:
            if item.raw is not None:
                blob_shas[item.path] = _write_blob(state.root, item.raw, deadline)

        _require_candidate_state(state, selected_paths, expected_head_sha, deadline)
        final_check = _prepare_all(state, validated_items, deadline)
        if not _same_prepared(rechecked, final_check):
            raise _fail(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "candidate state changed before index mutation",
            )

        payload = _index_info_payload(final_check, blob_shas)
        updated = _run_git(
            state.root,
            ["update-index", "-z", "--index-info"],
            deadline=deadline,
            stdin=payload,
        )
        if updated.returncode != 0:
            raise _fail(
                ContractErrorCode.INTERNAL_ERROR,
                "INDEX_UPDATE_FAILED",
                "Git index update failed",
            )

        current_head = _text(
            _run_git(state.root, ["rev-parse", "--verify", "HEAD^{commit}"], deadline=deadline),
            "HEAD_MISMATCH",
        )
        if current_head != expected_head_sha:
            raise _fail(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "HEAD changed after index mutation",
            )
        if _staged_paths(state.root, deadline) != selected_paths:
            raise _fail(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "staged path set does not equal the authorized candidate",
            )

        staged = diff_repository(str(state.root), "staged")
        if staged.full_diff_bytes == 0:
            raise _fail(
                ContractErrorCode.PRECONDITION_FAILED,
                "INDEX_UPDATE_FAILED",
                "authorized candidate produced an empty staged diff",
            )
        clean = _post_stage_clean(state.root, deadline)
        if not clean:
            raise _fail(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "worktree changed after index mutation",
            )

        path_results: list[RepoStagePathResult] = []
        for item in final_check:
            path_results.append(
                RepoStagePathResult(
                    path=item.path,
                    operation=item.operation,
                    worktree_sha256=item.sha256,
                    git_blob_sha=blob_shas.get(item.path),
                    git_mode=item.mode,
                )
            )
        return RepoStageResult(
            schema_version=1,
            branch=branch,
            head_sha=expected_head_sha,
            staged_paths=path_results,
            staged_diff_receipt=ReceiptV1Result(
                schema_version=staged.diff_receipt.schema_version,
                kind="repo-diff",
                digest=staged.diff_receipt.digest,
            ),
            post_stage_clean=True,
            network_used=False,
        )
    finally:
        os.close(state.root_fd)
