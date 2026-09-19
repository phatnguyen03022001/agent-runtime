from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictStr,
    model_serializer,
)

ARGV_MAX_ITEMS = 128
ARGV_ITEM_MAX_BYTES = 16 * 1024
ARGV_TOTAL_MAX_BYTES = 256 * 1024
TERMINAL_DATA_MAX_BYTES = 64 * 1024
SESSION_ID_MAX_CHARS = 128
FS_READ_MAX_LINE = 2_147_483_647


def _utf8_size(value: str, field_name: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc


def _validate_argv_item(value: str) -> str:
    if _utf8_size(value, "argv item") > ARGV_ITEM_MAX_BYTES:
        raise ValueError("argv item exceeds 16 KiB UTF-8 bytes")
    return value


def _validate_argv_total(value: list[str]) -> list[str]:
    if sum(_utf8_size(item, "argv item") for item in value) > ARGV_TOTAL_MAX_BYTES:
        raise ValueError("aggregate argv content exceeds 256 KiB UTF-8 bytes")
    return value


def _validate_terminal_data(value: str) -> str:
    if _utf8_size(value, "terminal data") > TERMINAL_DATA_MAX_BYTES:
        raise ValueError("terminal write data exceeds 64 KiB UTF-8 bytes")
    return value


ArgvItem = Annotated[
    StrictStr,
    Field(max_length=ARGV_ITEM_MAX_BYTES),
    AfterValidator(_validate_argv_item),
]
Argv = Annotated[
    list[ArgvItem],
    Field(strict=True, min_length=1, max_length=ARGV_MAX_ITEMS),
    AfterValidator(_validate_argv_total),
]
AbsoluteCwd = Annotated[
    StrictStr,
    Field(min_length=1),
]
TimeoutSeconds = Annotated[float, Field(strict=True, gt=0, le=3600)]
SessionId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=SESSION_ID_MAX_CHARS),
]
Cursor = Annotated[int, Field(strict=True, ge=0)]
WaitMilliseconds = Annotated[int, Field(strict=True, ge=0, le=1000)]
ControlAction = Literal["write", "interrupt", "terminate"]
TerminalWriteData = Annotated[
    StrictStr,
    Field(max_length=TERMINAL_DATA_MAX_BYTES),
    AfterValidator(_validate_terminal_data),
]
TerminalData = TerminalWriteData | None
TerminalDimension = Annotated[int, Field(strict=True, ge=1, le=65535)]
FsReadPath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsReadLine = Annotated[int, Field(strict=True, ge=1, le=FS_READ_MAX_LINE)]
FsReadMessage = Annotated[StrictStr, Field(max_length=160)]


class _ClosedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TerminalExecResult(_ClosedResult):
    cwd: str
    argv: list[str]
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class TerminalSessionResult(_ClosedResult):
    session_id: str
    status: Literal["running", "exited"]
    output: str
    next_cursor: int
    cursor_expired: bool
    dropped_output_bytes: int
    exit_code: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        data = handler(self)
        if self.status == "running":
            data.pop("exit_code", None)
        return data


class TerminalControlResult(_ClosedResult):
    session_id: str
    status: Literal["running", "exited"]
    exit_code: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        data = handler(self)
        if self.status == "running":
            data.pop("exit_code", None)
        return data


class CapacitySignalsAvailable(_ClosedResult):
    active_processors: int
    load1: float
    cpu_busy_pct: float
    sampled_window_ms: int
    thermal_state: str
    swap_used_bytes: int
    swap_total_bytes: int
    swapin_delta_pages: int
    swapout_delta_pages: int
    vm_free_bytes: int
    vm_inactive_bytes: int
    vm_purgeable_bytes: int
    vm_compressor_bytes: int
    disk_available_bytes: int


class CapacitySignalsUnavailable(_ClosedResult):
    probe_status: Literal["unavailable"]
    sampled_window_ms: int


CapacitySignals = CapacitySignalsAvailable | CapacitySignalsUnavailable


class CapacityObserverResult(_ClosedResult):
    schema_version: Literal[1]
    capacity_parallelism_ceiling: int
    reason_codes: list[str]
    signals: CapacitySignals


class FsReadItem(_ClosedResult):
    path: FsReadPath
    start_line: FsReadLine | None = None
    end_line: FsReadLine | None = None


FsReadItems = Annotated[list[FsReadItem], Field(strict=True, min_length=1, max_length=20)]
FsReadErrorCode = Literal[
    "NOT_FOUND",
    "ACCESS_DENIED",
    "SYMLINK_DISALLOWED",
    "NOT_REGULAR_FILE",
    "INVALID_UTF8",
    "ITEM_OUTPUT_LIMIT_EXCEEDED",
    "BATCH_OUTPUT_LIMIT_EXCEEDED",
    "ITEM_SCAN_LIMIT_EXCEEDED",
    "BATCH_SCAN_LIMIT_EXCEEDED",
    "READ_FAILED",
]


class FsReadOkResult(_ClosedResult):
    status: Literal["ok"]
    path: str
    start_line: int
    end_line: int | None
    text: str


class FsReadErrorResult(_ClosedResult):
    status: Literal["error"]
    path: str
    start_line: int
    end_line: int | None
    error_code: FsReadErrorCode
    message: FsReadMessage


FsReadItemResult = Annotated[FsReadOkResult | FsReadErrorResult, Field(discriminator="status")]


class FsReadBatchResult(_ClosedResult):
    items: list[FsReadItemResult]


RepoObserverMaxPaths = Annotated[int, Field(strict=True, ge=1, le=1000)]
RepoFastForwardBranch = Annotated[StrictStr, Field(min_length=1, max_length=255)]
RepoFastForwardSha = Annotated[
    StrictStr,
    Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$"),
]
RepoPublishBranch = RepoFastForwardBranch
RepoPublishSha = RepoFastForwardSha
TypedToolErrorCode = Literal[
    "INVALID_ARGUMENT",
    "OUTSIDE_WORKSPACE",
    "NOT_GIT_REPOSITORY",
    "NOT_REPOSITORY_ROOT",
    "DIRTY_WORKTREE",
    "OPERATION_IN_PROGRESS",
    "DETACHED_HEAD",
    "BRANCH_MISMATCH",
    "UPSTREAM_MISMATCH",
    "LOCAL_HEAD_MISMATCH",
    "REMOTE_HEAD_MISMATCH",
    "NON_FAST_FORWARD",
    "LOCAL_STATE_CHANGED",
    "FETCH_FAILED",
    "OUTPUT_LIMIT",
    "DEADLINE_EXCEEDED",
    "TRANSIENT_FAILURE",
    "FAST_FORWARD_FAILED",
    "PUBLICATION_LINEAGE_MISMATCH",
    "PUSH_FAILED",
    "PUBLICATION_AMBIGUOUS",
    "INTERNAL_ERROR",
]
RepoChangeStatus = Literal["M", "T", "A", "D", "R", "C", "U", "?", "!"]


class TypedToolErrorPayload(_ClosedResult):
    code: TypedToolErrorCode
    message: Annotated[StrictStr, Field(max_length=256)]
    retryable: bool


class TypedToolErrorEnvelope(_ClosedResult):
    error: TypedToolErrorPayload


class RepoRepository(_ClosedResult):
    root: str
    cwd: str
    bare: bool
    shallow: bool
    inside_workspace_root: Literal[True]
    cwd_inside_repo: Literal[True]
    cwd_is_repo_root: bool


class RepoBranch(_ClosedResult):
    head_sha: str | None
    name: str | None
    detached: bool


class RepoTracking(_ClosedResult):
    upstream: str | None
    tracking_sha: str | None
    tracking_known: bool
    ahead: int | None
    behind: int | None


class RepoChange(_ClosedResult):
    path: str
    original_path: str | None
    index_status: RepoChangeStatus | None
    worktree_status: RepoChangeStatus | None
    tracked: bool
    staged: bool
    conflicted: bool


class RepoDiffSummary(_ClosedResult):
    staged_files: int | None
    unstaged_files: int | None
    untracked_files: int | None
    conflicted_files: int | None
    additions: int | None
    deletions: int | None
    exact: bool


class RepoOperationState(_ClosedResult):
    merge: bool
    rebase: bool
    cherry_pick: bool
    bisect: bool


class RepoWorktree(_ClosedResult):
    path: str
    head_sha: str | None
    branch: str | None
    detached: bool
    bare: bool
    locked: bool
    prunable: bool


class RepoWorktrees(_ClosedResult):
    entries: list[RepoWorktree]
    outside_workspace_count: int
    total_count: int | None
    total_exact: bool


class RepoObservation(_ClosedResult):
    fetched: Literal[False]
    network_used: Literal[False]
    deadline_seconds: float


class RepoTruncation(_ClosedResult):
    changes_truncated: bool
    worktrees_truncated: bool
    diff_truncated: bool
    total_changes: int | None
    total_changes_exact: bool


class RepoObserverResult(_ClosedResult):
    schema_version: Literal[1]
    repository: RepoRepository
    branch: RepoBranch
    tracking: RepoTracking
    changes: list[RepoChange]
    diff_summary: RepoDiffSummary
    operation_state: RepoOperationState
    worktrees: RepoWorktrees
    observation: RepoObservation
    truncation: RepoTruncation


class RepoFastForwardResult(_ClosedResult):
    schema_version: Literal[1]
    status: Literal["fast_forwarded", "already_at_target"]
    repository_root: str
    branch: str
    remote: Literal["origin"]
    upstream: str
    expected_local_head: str
    expected_remote_head: str
    head_before: str
    head_after: str
    tracking_head: str
    fetched: Literal[True]
    network_used: Literal[True]
    fast_forwarded: bool
    deadline_seconds: float


class RepoPublishResult(_ClosedResult):
    schema_version: Literal[1]
    status: Literal["published", "already_published"]
    repository_root: str
    branch: str
    remote: Literal["origin"]
    upstream: str
    expected_remote_head: str
    commit: str
    head: str
    remote_head_before: str
    remote_head_after: str
    network_used: Literal[True]
    push_attempted: bool
    published: bool
    deadline_seconds: float
