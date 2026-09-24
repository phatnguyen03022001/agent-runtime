from __future__ import annotations

import hashlib
import os
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .tool_contract import (
    Authority,
    CONTINUATION_CURSOR_MAX_CHARS,
    CONTINUATION_TTL_SECONDS,
    ContinuationFailure,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
    canonical_structured_bytes,
    frame_bytes,
    make_continuation_cursor,
    make_receipt_v1,
    parse_continuation_cursor,
)

from .contracts import (
    ContinuationReceiptResult,
    RepoBranch,
    RepoChange,
    RepoDiffSummary,
    RepoObservation,
    RepoOperationState,
    RepoObserverResult,
    RepoRepository,
    RepoTracking,
    RepoTruncation,
    RepoWorktree,
    RepoWorktrees,
    TypedToolErrorCode,
)

GIT_EXECUTABLE = "/usr/bin/git"
CALL_DEADLINE_SECONDS = 5.0
MAX_PATHS_DEFAULT = 200
MAX_PATHS_LIMIT = 1000

_STATUS_MAX_BYTES = 512 * 1024
_DIFF_MAX_BYTES = 256 * 1024
_WORKTREE_MAX_BYTES = 128 * 1024
_TEXT_MAX_BYTES = 16 * 1024
_STDERR_MAX_BYTES = 16 * 1024
_MAX_WORKTREE_ENTRIES = 64

_NETWORK_VERBS = frozenset(
    {"fetch", "pull", "push", "clone", "ls-remote", "archive", "submodule"}
)
_STATUS_VALUES = frozenset({"M", "T", "A", "D", "R", "C", "U", "?", "!"})

REPO_OBSERVER_CONTRACT = ToolContract(
    name="repo_observer",
    tool_class=ToolClass.REPO,
    authority=Authority(True, NetworkAuthority.NONE, MutationAuthority.NONE),
    annotations=ToolAnnotations(True, False, True, False),
    preconditions={"cwd": "workspace-contained-git-working-tree", "network_verbs": "forbidden"},
    bounds={
        "max_paths": MAX_PATHS_LIMIT,
        "status_bytes": _STATUS_MAX_BYTES,
        "diff_bytes": _DIFF_MAX_BYTES,
        "worktree_bytes": _WORKTREE_MAX_BYTES,
        "deadline_milliseconds": int(CALL_DEADLINE_SECONDS * 1000),
        "continuation_cursor_chars": CONTINUATION_CURSOR_MAX_CHARS,
        "continuation_ttl_seconds": CONTINUATION_TTL_SECONDS,
    },
    postconditions={
        "fetched": False,
        "network_used": False,
        "repository_mutation": False,
        "continuation_consistency": "local-observation-revalidated",
    },
)

class RepoObserverFailure(Exception):
    def __init__(
        self,
        code: TypedToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        contract_code: ContractErrorCode | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:256]
        self.retryable = retryable
        self.contract_code = contract_code
        self.reason_code = reason_code or code


@dataclass(frozen=True, slots=True)
class _RunResult:
    returncode: int
    stderr: str
    output_truncated: bool


@dataclass(slots=True)
class _StatusState:
    head_sha: str | None = None
    branch_name: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    total_changes: int = 0
    untracked_files: int = 0
    conflicted_files: int = 0
    retained: list[dict[str, object]] | None = None
    awaiting_original_for: int | None = None

    def __post_init__(self) -> None:
        if self.retained is None:
            self.retained = []


@dataclass(slots=True)
class _DiffState:
    files: int = 0
    additions: int = 0
    deletions: int = 0
    numeric_exact: bool = True
    binary_seen: bool = False


@dataclass(slots=True)
class _WorktreeBuilder:
    path: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass(slots=True)
class _WorktreeState:
    entries: list[RepoWorktree] | None = None
    outside_workspace_count: int = 0
    total_count: int = 0
    retained_truncated: bool = False
    current: _WorktreeBuilder | None = None

    def __post_init__(self) -> None:
        if self.entries is None:
            self.entries = []


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RepoObserverFailure(
            "DEADLINE_EXCEEDED",
            "repo_observer exceeded its 5-second call deadline",
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


def _git_argv(args: list[str], *, diff_probe: bool) -> list[str]:
    if not args or args[0] in _NETWORK_VERBS:
        raise RepoObserverFailure("INTERNAL_ERROR", "unsafe internal Git operation")
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
    ]
    if diff_probe:
        argv.extend(
            [
                "-c",
                "diff.external=",
            ]
        )
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


def _run_git_records(
    cwd: Path,
    args: list[str],
    *,
    remaining_time: float,
    max_stdout_bytes: int,
    delimiter: bytes,
    on_record: Callable[[bytes], None],
    diff_probe: bool = False,
) -> _RunResult:
    if remaining_time <= 0:
        raise RepoObserverFailure(
            "DEADLINE_EXCEEDED",
            "repo_observer exceeded its 5-second call deadline",
            retryable=True,
        )
    if len(delimiter) != 1:
        raise RepoObserverFailure("INTERNAL_ERROR", "invalid internal record delimiter")

    local_deadline = time.monotonic() + remaining_time
    argv = _git_argv(args, diff_probe=diff_probe)
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
            shell=False,
        )
    except OSError as exc:
        raise RepoObserverFailure(
            "INTERNAL_ERROR",
            "unable to start fixed local Git executable",
        ) from exc

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    stdout_seen = 0
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_truncated = False

    try:
        while selector.get_map():
            remaining = local_deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise RepoObserverFailure(
                    "DEADLINE_EXCEEDED",
                    "repo_observer exceeded its 5-second call deadline",
                    retryable=True,
                )

            events = selector.select(timeout=min(remaining, 0.1))
            if not events:
                if process.poll() is not None:
                    # Drain EOF notifications on the next selector turn.
                    continue
                continue

            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except OSError as exc:
                    _terminate_process(process)
                    raise RepoObserverFailure(
                        "TRANSIENT_FAILURE",
                        "local Git output could not be read",
                        retryable=True,
                    ) from exc

                if not chunk:
                    selector.unregister(key.fileobj)
                    continue

                if key.data == "stderr":
                    remaining_stderr = _STDERR_MAX_BYTES - len(stderr_buffer)
                    if remaining_stderr > 0:
                        stderr_buffer.extend(chunk[:remaining_stderr])
                    continue

                allowed = max_stdout_bytes - stdout_seen
                if allowed <= 0:
                    output_truncated = True
                    _terminate_process(process)
                    break

                accepted = chunk[:allowed]
                stdout_seen += len(accepted)
                stdout_buffer.extend(accepted)

                while True:
                    index = stdout_buffer.find(delimiter)
                    if index < 0:
                        break
                    record = bytes(stdout_buffer[:index])
                    del stdout_buffer[: index + 1]
                    on_record(record)

                if len(chunk) > allowed:
                    output_truncated = True
                    _terminate_process(process)
                    break

            if output_truncated:
                break

        if not output_truncated and stdout_buffer:
            on_record(bytes(stdout_buffer))
            stdout_buffer.clear()

        if process.poll() is None:
            remaining = local_deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise RepoObserverFailure(
                    "DEADLINE_EXCEEDED",
                    "repo_observer exceeded its 5-second call deadline",
                    retryable=True,
                )
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                _terminate_process(process)
                raise RepoObserverFailure(
                    "DEADLINE_EXCEEDED",
                    "repo_observer exceeded its 5-second call deadline",
                    retryable=True,
                ) from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    stderr = bytes(stderr_buffer).decode("utf-8", "replace").strip()
    return _RunResult(
        returncode=int(process.returncode or 0),
        stderr=stderr,
        output_truncated=output_truncated,
    )


def _run_git_text(
    cwd: Path,
    args: list[str],
    *,
    remaining_time: float,
    max_stdout_bytes: int = _TEXT_MAX_BYTES,
) -> tuple[_RunResult, list[str]]:
    records: list[str] = []

    def collect(record: bytes) -> None:
        records.append(record.decode("utf-8", "replace"))

    result = _run_git_records(
        cwd,
        args,
        remaining_time=remaining_time,
        max_stdout_bytes=max_stdout_bytes,
        delimiter=b"\n",
        on_record=collect,
    )
    if result.output_truncated:
        raise RepoObserverFailure(
            "OUTPUT_LIMIT",
            "bounded local Git metadata exceeded its output limit",
        )
    return result, records


def _workspace_root() -> Path:
    raw = os.environ.get("AGENT_RUNTIME_WORKSPACE_ROOT", "")
    if not raw:
        raise RepoObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is unavailable",
        )
    path = Path(raw)
    if not path.is_absolute():
        raise RepoObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is invalid",
      )
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise RepoObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is unavailable",
        ) from exc
    if not stat.S_ISDIR((mode)):
        raise RepoObserverFailure(
            "INTERNAL_ERROR",
            "AGENT_RUNTIME_WORKSPACE_ROOT is invalid",
        )
    return resolved


def _validate_cwd(raw_cwd: str, workspace_root: Path) -> Path:
    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise RepoObserverFailure("INVALID_ARGUMENT", "cwd must be a non-empty absolute path")
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise RepoObserverFailure("INVALID_ARGUMENT", "cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise RepoObserverFailure(
            "INVALID_ARGUMENT",
            "cwd must identify an existing directory",
        ) from exc
    if not stat.S_ISDIR((mode)):
        raise RepoObserverFailure("INVALID_ARGUMENT", "cwd must identify an existing directory")
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise RepoObserverFailure(
            "OUTSIDE_WORKSPACE",
            "cwd resolves outside AGENT_RUNTIME_WORKSPACE_ROOT",
        ) from exc
    return resolved


def _require_git_success(result: _RunResult, *, not_repo_ok: bool = False) -> None:
    if result.returncode == 0:
        return
    lowered = result.stderr.lower()
    if not_repo_ok and (
        "not a git repository" in lowered
        or "not a git directory" in lowered
        or "must be run in a work tree" in lowered
    ):
        raise RepoObserverFailure(
            "NOT_GIT_REPOSITORY",
            "cwd is not inside a supported Git working tree",
        )
    raise RepoObserverFailure(
        "TRANSIENT_FAILURE",
        "local Git observation failed",
        retryable=True,
    )


def _normalize_status(value: str) -> str | None:
    if value == ".":
        return None
    if value not in _STATUS_VALUES:
        raise RepoObserverFailure(
            "INTERNAL_ERROR",
            "unexpected Git porcelain status code",
        )
    return value


def _decode_path(value: bytes) -> str:
    return os.fsdecode(value)


def _status_parser(state: _StatusState, max_paths: int, offset: int = 0) -> Callable[[bytes], None]:
    def parse(record: bytes) -> None:
        if state.awaiting_original_for is not None:
            if state.awaiting_original_for >= 0:
                assert state.retained is not None
                state.retained[state.awaiting_original_for]["original_path"] = _decode_path(record)
            state.awaiting_original_for = None
            return

        if not record:
            return
        if record.startswith(b"# "):
            text = record.decode("utf-8", "replace")
            if text.startswith("# branch.oid "):
                oid = text[len("# branch.oid ") :]
                state.head_sha = None if oid == "(initial)" else oid
            elif text.startswith("# branch.head "):
                name = text[len("# branch.head ") :]
                if name in {"(detached)", "(unknown)"}:
                    state.detached = True
                    state.branch_name = None
                else:
                    state.branch_name = name
                    state.detached = False
            elif text.startswith("# branch.upstream "):
                state.upstream = text[len("# branch.upstream ") :]
            elif text.startswith("# branch.ab "):
                fields = text[len("# branch.ab ") :].split()
                if len(fields) == 2:
                    try:
                        state.ahead = int(fields[0].lstrip("+"))
                        state.behind = int(fields[1].lstrip("-"))
                    except ValueError:
                        state.ahead = None
                        state.behind = None
            return

        payload: dict[str, object] | None = None
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9:
                return
            xy = parts[1].decode("ascii", "replace")
            index_status = _normalize_status(xy[0])
            worktree_status = _normalize_status(xy[1])
            conflicted = index_status == "U" or worktree_status == "U"
            payload = {
                "path": _decode_path(parts[8]),
                "original_path": None,
                "index_status": index_status,
                "worktree_status": worktree_status,
                "tracked": True,
                "staged": index_status is not None,
                "conflicted": conflicted,
            }
        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10:
                return
            xy = parts[1].decode("ascii", "replace")
            index_status = _normalize_status(xy[0])
            worktree_status = _normalize_status(xy[1])
            payload = {
                "path": _decode_path(parts[9]),
                "original_path": None,
                "index_status": index_status,
                "worktree_status": worktree_status,
                "tracked": True,
                "staged": index_status is not None,
                "conflicted": index_status == "U" or worktree_status == "U",
            }
        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11:
                return
            xy = parts[1].decode("ascii", "replace")
            payload = {
                "path": _decode_path(parts[10]),
                "original_path": None,
                "index_status": _normalize_status(xy[0]),
                "worktree_status": _normalize_status(xy[1]),
                "tracked": True,
                "staged": True,
                "conflicted": True,
            }
        elif record.startswith(b"? "):
            payload = {
                "path": _decode_path(record[2:]),
                "original_path": None,
                "index_status": None,
                "worktree_status": "?",
                "tracked": False,
                "staged": False,
                "conflicted": False,
            }
        else:
            return

        state.total_changes += 1
        if payload["tracked"] is False:
            state.untracked_files += 1
        if payload["conflicted"] is True:
            state.conflicted_files += 1

        retained_index = -1
        assert state.retained is not None
        change_index = state.total_changes - 1
        if offset <= change_index < offset + max_paths:
            state.retained.append(payload)
            retained_index = len(state.retained) - 1
        if record.startswith(b"2 "):
            state.awaiting_original_for = retained_index

    return parse


def _diff_parser(state: _DiffState) -> Callable[[bytes], None]:
    def parse(record: bytes) -> None:
        if not record:
            return
        # With --numstat -z, rename/copy path continuations are separate NUL
        # records without tabs. They carry no additional numeric information.
        if b"\t" not in record:
            return
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise RepoObserverFailure("INTERNAL_ERROR", "malformed Git numstat record")
        added = fields[0]
        deleted = fields[1]
        state.files += 1
        if added.isdigit() and deleted.isdigit():
            state.additions += int(added)
            state.deletions += int(deleted)
            return
        if added == b"-" and deleted == b"-":
            # Valid Git representation for a binary-file numstat record.
            state.binary_seen = True
            state.numeric_exact = False
            return
        raise RepoObserverFailure("INTERNAL_ERROR", "unexpected Git numstat record")

    return parse


def _tracking_state(
    repo_root: Path,
    status: _StatusState,
    *,
    deadline: float,
) -> RepoTracking:
    if status.upstream is None:
        return RepoTracking(
            upstream=None,
            tracking_sha=None,
            tracking_known=False,
            ahead=None,
            behind=None,
        )

    result, records = _run_git_text(
        repo_root,
        ["rev-parse", "--verify", "--quiet", "@{upstream}^{commit}"],
        remaining_time=_remaining(deadline),
    )
    if result.returncode == 0:
        if len(records) != 1 or not records[0].strip():
            raise RepoObserverFailure(
                "INTERNAL_ERROR",
                "local Git tracking-ref probe was malformed",
            )
        return RepoTracking(
            upstream=status.upstream,
            tracking_sha=records[0].strip(),
            tracking_known=True,
            ahead=status.ahead,
            behind=status.behind,
        )
    if result.returncode in {1, 128}:
        return RepoTracking(
            upstream=status.upstream,
            tracking_sha=None,
            tracking_known=False,
            ahead=None,
            behind=None,
        )
    raise RepoObserverFailure(
        "TRANSIENT_FAILURE",
        "local Git tracking-ref observation failed",
        retryable=True,
    )


def _path_inside_workspace_without_inspection(path: str, workspace_root: Path) -> bool:
    if not os.path.isabs(path):
        return False
    normalized = os.path.normpath(path)
    try:
        return os.path.commonpath((str(workspace_root), normalized)) == str(workspace_root)
    except ValueError:
        return False


def _finish_worktree(
    state: _WorktreeState,
    workspace_root: Path,
) -> None:
    current = state.current
    if current is None or current.path is None:
        state.current = None
        return

    state.total_count += 1
    if not _path_inside_workspace_without_inspection(current.path, workspace_root):
        state.outside_workspace_count += 1
        state.current = None
        return

    try:
        resolved = Path(current.path).resolve(strict=False)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError):
        state.outside_workspace_count += 1
        state.current = None
        return

    assert state.entries is not None
    if len(state.entries) >= _MAX_WORKTREE_ENTRIES:
        state.retained_truncated = True
        state.current = None
        return

    state.entries.append(
        RepoWorktree(
            path=str(resolved),
            head_sha=current.head_sha,
            branch=current.branch,
            detached=current.detached,
            bare=current.bare,
            locked=current.locked,
            prunable=current.prunable,
        )
    )
    state.current = None


def _worktree_parser(
    state: _WorktreeState,
    workspace_root: Path,
) -> Callable[[bytes], None]:
    def parse(record: bytes) -> None:
        if not record:
            _finish_worktree(state, workspace_root)
            return
        text = record.decode("utf-8", "replace")
        if text.startswith("worktree "):
            if state.current is not None:
                _finish_worktree(state, workspace_root)
            state.current = _WorktreeBuilder(path=text[len("worktree ") :])
            return
        if state.current is None:
            return
        if text.startswith("HEAD "):
            state.current.head_sha = text[len("HEAD ") :]
        elif text.startswith("branch "):
            state.current.branch = text[len("branch ") :]
        elif text == "detached":
            state.current.detached = True
        elif text == "bare":
            state.current.bare = True
        elif text.startswith("locked"):
            state.current.locked = True
        elif text.startswith("prunable"):
            state.current.prunable = True

    return parse


def _operation_state(
    repo_root: Path,
    workspace_root: Path,
    *,
    deadline: float,
) -> RepoOperationState:
    result, records = _run_git_text(
        repo_root,
        [
            "rev-parse",
            "--git-path",
            "MERGE_HEAD",
            "--git-path",
            "rebase-merge",
            "--git-path",
            "rebase-apply",
            "--git-path",
            "CHERRY_PICK_HEAD",
            "--git-path",
            "BISECT_LOG",
        ],
        remaining_time=_remaining(deadline),
    )
    _require_git_success(result)
    if len(records) != 5:
        raise RepoObserverFailure("INTERNAL_ERROR", "local Git operation-state probe was malformed")

    existence: list[bool] = []
    for raw in records:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        normalized = os.path.normpath(str(candidate))
        if not _path_inside_workspace_without_inspection(normalized, workspace_root):
            existence.append(False)
            continue
        try:
            resolved = Path(normalized).resolve(strict=False)
            resolved.relative_to(workspace_root)
        except (OSError, ValueError):
            existence.append(False)
            continue
        existence.append(resolved.exists())

    return RepoOperationState(
        merge=existence[0],
        rebase=existence[1] or existence[2],
        cherry_pick=existence[3],
        bisect=existence[4],
    )


def observe_repository(
    cwd: str,
    max_paths: int = MAX_PATHS_DEFAULT,
    cursor: str | None = None,
    continuation_receipt: ContinuationReceiptResult | None = None,
) -> RepoObserverResult:
    if type(max_paths) is not int or not (1 <= max_paths <= MAX_PATHS_LIMIT):
        raise RepoObserverFailure(
            "INVALID_ARGUMENT",
            f"max_paths must be an integer from 1 to {MAX_PATHS_LIMIT}",
        )
    if (cursor is None) != (continuation_receipt is None):
        raise RepoObserverFailure(
            "INVALID_ARGUMENT",
            "cursor and continuation_receipt must be provided together",
            contract_code=ContractErrorCode.INVALID_ARGUMENT,
            reason_code="CONTINUATION_PAIR_REQUIRED",
        )

    semantic_parameters = {"max_paths": max_paths}
    offset = 0
    if continuation_receipt is not None:
        if continuation_receipt.kind != "repo-observer":
            raise RepoObserverFailure(
                "INVALID_ARGUMENT",
                "continuation receipt belongs to another tool",
                contract_code=ContractErrorCode.INVALID_ARGUMENT,
                reason_code="CONTINUATION_RECEIPT_KIND_MISMATCH",
            )
        try:
            offset = parse_continuation_cursor(
                cursor or "",
                tool="repo_observer",
                semantic_parameters=semantic_parameters,
                receipt_digest=continuation_receipt.digest,
            )
        except ContinuationFailure as exc:
            raise RepoObserverFailure(
                "INVALID_ARGUMENT",
                exc.message,
                contract_code=(
                    ContractErrorCode.PRECONDITION_FAILED
                    if exc.reason_code == "CONTINUATION_EXPIRED"
                    else ContractErrorCode.INVALID_ARGUMENT
                ),
                reason_code=exc.reason_code,
            ) from None

    deadline = time.monotonic() + CALL_DEADLINE_SECONDS
    workspace_root = _workspace_root()
    checked_cwd = _validate_cwd(cwd, workspace_root)

    repo_probe, repo_probe_lines = _run_git_text(
        checked_cwd,
        ["rev-parse", "--is-bare-repository", "--is-inside-work-tree"],
        remaining_time=_remaining(deadline),
    )
    _require_git_success(repo_probe, not_repo_ok=True)
    if len(repo_probe_lines) != 2:
        raise RepoObserverFailure("INTERNAL_ERROR", "local Git repository probe was malformed")
    bare = repo_probe_lines[0].strip() == "true"
    inside_work_tree = repo_probe_lines[1].strip() == "true"
    if bare:
        raise RepoObserverFailure(
            "INVALID_ARGUMENT",
            "bare Git repositories are not supported by repo_observer",
        )
    if not inside_work_tree:
        raise RepoObserverFailure(
            "NOT_GIT_REPOSITORY",
            "cwd is not inside a supported Git working tree",
        )

    root_probe, root_lines = _run_git_text(
        checked_cwd,
        ["rev-parse", "--show-toplevel", "--is-shallow-repository"],
        remaining_time=_remaining(deadline),
    )
    _require_git_success(root_probe, not_repo_ok=True)
    if len(root_lines) != 2:
        raise RepoObserverFailure("INTERNAL_ERROR", "local Git root probe was malformed")
    try:
        repo_root = Path(root_lines[0]).resolve(strict=True)
        repo_root.relative_to(workspace_root)
    except (OSError, ValueError) as exc:
        raise RepoObserverFailure(
            "OUTSIDE_WORKSPACE",
            "repository root resolves outside AGENT_RUNTIME_WORKSPACE_ROOT",
        ) from exc
    shallow = root_lines[1].strip() == "true"

    def hashed_parser(hasher, parser):
        def consume(record: bytes) -> None:
            hasher.update(frame_bytes(record))
            parser(record)

        return consume

    status_hasher = hashlib.sha256()
    status_state = _StatusState()
    status_result = _run_git_records(
        repo_root,
        [
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ],
        remaining_time=_remaining(deadline),
        max_stdout_bytes=_STATUS_MAX_BYTES,
        delimiter=b"\0",
        on_record=hashed_parser(status_hasher, _status_parser(status_state, max_paths, offset)),
    )
    if not status_result.output_truncated:
        _require_git_success(status_result, not_repo_ok=True)

    tracking_state = _tracking_state(
        repo_root,
        status_state,
        deadline=deadline,
    )

    staged_hasher = hashlib.sha256()
    staged_diff = _DiffState()
    staged_result = _run_git_records(
        repo_root,
        [
            "diff",
            "--cached",
            "--numstat",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--",
        ],
        remaining_time=_remaining(deadline),
        max_stdout_bytes=_DIFF_MAX_BYTES,
        delimiter=b"\0",
        on_record=hashed_parser(staged_hasher, _diff_parser(staged_diff)),
        diff_probe=True,
    )
    if not staged_result.output_truncated:
        _require_git_success(staged_result)

    unstaged_hasher = hashlib.sha256()
    unstaged_diff = _DiffState()
    unstaged_result = _run_git_records(
        repo_root,
        [
            "diff",
            "--numstat",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--",
        ],
        remaining_time=_remaining(deadline),
        max_stdout_bytes=_DIFF_MAX_BYTES,
        delimiter=b"\0",
        on_record=hashed_parser(unstaged_hasher, _diff_parser(unstaged_diff)),
        diff_probe=True,
    )
    if not unstaged_result.output_truncated:
        _require_git_success(unstaged_result)

    operation_state = _operation_state(
        repo_root,
        workspace_root,
        deadline=deadline,
    )

    worktree_hasher = hashlib.sha256()
    worktree_state = _WorktreeState()
    worktree_result = _run_git_records(
        repo_root,
        ["worktree", "list", "--porcelain", "-z"],
        remaining_time=_remaining(deadline),
        max_stdout_bytes=_WORKTREE_MAX_BYTES,
        delimiter=b"\0",
        on_record=hashed_parser(
            worktree_hasher,
            _worktree_parser(worktree_state, workspace_root),
        ),
    )
    if not worktree_result.output_truncated:
        _require_git_success(worktree_result)
        _finish_worktree(worktree_state, workspace_root)

    status_exact = not status_result.output_truncated and status_state.awaiting_original_for is None
    diff_truncated = staged_result.output_truncated or unstaged_result.output_truncated
    diff_exact = (
        not diff_truncated
        and staged_diff.numeric_exact
        and unstaged_diff.numeric_exact
        and status_exact
    )
    additions: int | None
    deletions: int | None
    if staged_diff.numeric_exact and unstaged_diff.numeric_exact and not diff_truncated:
        additions = staged_diff.additions + unstaged_diff.additions
        deletions = staged_diff.deletions + unstaged_diff.deletions
    else:
        additions = None
        deletions = None

    retained = status_state.retained or []
    changes = [RepoChange(**payload) for payload in retained]
    total_changes = status_state.total_changes if status_exact else None
    if total_changes is not None and offset > total_changes:
        raise RepoObserverFailure(
            "INVALID_ARGUMENT",
            "continuation cursor position is outside the current change collection",
            contract_code=ContractErrorCode.INVALID_ARGUMENT,
            reason_code="CONTINUATION_POSITION_INVALID",
        )
    more_changes = total_changes is not None and offset + len(changes) < total_changes
    changes_truncated = (
        status_result.output_truncated or offset > 0 or more_changes
    )

    worktrees_exact = not worktree_result.output_truncated
    worktrees_truncated = worktree_result.output_truncated or worktree_state.retained_truncated

    repository_result = RepoRepository(
        root=str(repo_root),
        cwd=str(checked_cwd),
        bare=False,
        shallow=shallow,
        inside_workspace_root=True,
        cwd_inside_repo=True,
        cwd_is_repo_root=checked_cwd == repo_root,
    )
    branch_result = RepoBranch(
        head_sha=status_state.head_sha,
        name=status_state.branch_name,
        detached=status_state.detached,
    )
    diff_summary_result = RepoDiffSummary(
        staged_files=staged_diff.files if not staged_result.output_truncated else None,
        unstaged_files=unstaged_diff.files if not unstaged_result.output_truncated else None,
        untracked_files=status_state.untracked_files if status_exact else None,
        conflicted_files=status_state.conflicted_files if status_exact else None,
        additions=additions,
        deletions=deletions,
        exact=diff_exact,
    )
    worktrees_result = RepoWorktrees(
        entries=worktree_state.entries or [],
        outside_workspace_count=worktree_state.outside_workspace_count,
        total_count=worktree_state.total_count if worktrees_exact else None,
        total_exact=worktrees_exact,
    )
    observation_result = RepoObservation(
        fetched=False,
        network_used=False,
        deadline_seconds=CALL_DEADLINE_SECONDS,
    )
    truncation_result = RepoTruncation(
        changes_truncated=changes_truncated,
        worktrees_truncated=worktrees_truncated,
        diff_truncated=diff_truncated,
        total_changes=total_changes,
        total_changes_exact=status_exact,
    )

    receipt = make_receipt_v1(
        kind="repo-observer",
        subject={"repository_root": str(repo_root), "cwd": str(checked_cwd)},
        semantic_parameters=semantic_parameters,
        observed_state_bytes=canonical_structured_bytes(
            {
                "repository": repository_result.model_dump(),
                "branch": branch_result.model_dump(),
                "tracking": tracking_state.model_dump(),
                "status_digest": status_hasher.hexdigest(),
                "status_total_changes": total_changes,
                "staged_diff_digest": staged_hasher.hexdigest(),
                "unstaged_diff_digest": unstaged_hasher.hexdigest(),
                "diff_summary": diff_summary_result.model_dump(),
                "operation_state": operation_state.model_dump(),
                "worktree_digest": worktree_hasher.hexdigest(),
                "worktrees": worktrees_result.model_dump(),
                "status_output_truncated": status_result.output_truncated,
                "staged_output_truncated": staged_result.output_truncated,
                "unstaged_output_truncated": unstaged_result.output_truncated,
                "worktree_output_truncated": worktree_result.output_truncated,
            }
        ),
    )
    if continuation_receipt is not None and receipt.digest != continuation_receipt.digest:
        raise RepoObserverFailure(
            "LOCAL_STATE_CHANGED",
            "repository observation changed since the continuation receipt was issued",
            contract_code=ContractErrorCode.STATE_CHANGED,
            reason_code="CONTINUATION_STATE_CHANGED",
        )

    exact_resumable_identity = (
        status_exact
        and not staged_result.output_truncated
        and not unstaged_result.output_truncated
        and not worktree_result.output_truncated
    )
    receipt_result = ContinuationReceiptResult(
        schema_version=1,
        kind="repo-observer",
        digest=receipt.digest,
    )
    next_cursor = (
        make_continuation_cursor(
            tool="repo_observer",
            position=offset + len(changes),
            semantic_parameters=semantic_parameters,
            receipt_digest=receipt.digest,
        )
        if more_changes and exact_resumable_identity
        else None
    )
    top_level_truncated = changes_truncated or worktrees_truncated or diff_truncated

    return RepoObserverResult(
        schema_version=2,
        repository=repository_result,
        branch=branch_result,
        tracking=tracking_state,
        changes=changes,
        diff_summary=diff_summary_result,
        operation_state=operation_state,
        worktrees=worktrees_result,
        observation=observation_result,
        truncation=truncation_result,
        truncated=top_level_truncated,
        next_cursor=next_cursor,
        continuation_receipt=receipt_result,
    )
