from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    SerializerFunctionWrapHandler,
    StrictStr,
    model_serializer,
)

from .tool_contract import ContractErrorCode

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
ScreenCaptureTarget = Literal[
    "frontmost_window", "window", "application_window", "display", "region"
]
ScreenCaptureWindowId = Annotated[int, Field(strict=True, ge=1, le=4_294_967_295)]
ScreenCaptureDisplayId = Annotated[int, Field(strict=True, ge=1, le=4_294_967_295)]
ScreenCaptureBundleId = Annotated[StrictStr, Field(min_length=1, max_length=512)]
ScreenCaptureCoordinate = FiniteFloat
ScreenCaptureExtent = Annotated[FiniteFloat, Field(gt=0)]
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
    "SCREEN_CAPTURE_PERMISSION_REQUIRED",
    "CAPTURE_TARGET_NOT_FOUND",
    "CAPTURE_TARGET_AMBIGUOUS",
    "CAPTURE_PAYLOAD_TOO_LARGE",
    "CAPTURE_PROTOCOL_ERROR",
    "CAPTURE_HELPER_UNAVAILABLE",
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


class ScreenCaptureBounds(_ClosedResult):
    x: float
    y: float
    width: float
    height: float


class ScreenCaptureApplication(_ClosedResult):
    pid: int
    bundle_identifier: str | None
    name: str


class ScreenCaptureMetadata(_ClosedResult):
    schema_version: Literal[1]
    status: Literal["captured"]
    target: ScreenCaptureTarget
    mime_type: Literal["image/png"]
    raw_bytes: int
    sha256: str
    coordinate_space: Literal["cg_global_points"]
    bounds: ScreenCaptureBounds
    pixel_width: int
    pixel_height: int
    scale_factor: float
    display_id: int
    window_id: int | None
    active_application: ScreenCaptureApplication
    captured_application: ScreenCaptureApplication | None
    permission: Literal["granted"]
    capture_api: Literal["ScreenCaptureKit"]
    deadline_seconds: float

CapabilityReasonCode = Annotated[StrictStr, Field(min_length=1, max_length=128)]
CapabilityMessage = Annotated[StrictStr, Field(max_length=256)]
FsListPath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsListMaxEntries = Annotated[int, Field(strict=True, ge=1, le=1000)]
FsSearchQuery = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsSearchMode = Literal["content", "path"]
FsSearchRootPath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsSearchMaxResults = Annotated[int, Field(strict=True, ge=1, le=500)]
FsPatchPath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsPatchExpectedSha256 = Annotated[
    StrictStr,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
RepoDiffScope = Literal["worktree", "staged"]


class CapabilityErrorPayload(_ClosedResult):
    code: ContractErrorCode
    reason_code: CapabilityReasonCode
    message: CapabilityMessage
    retryable: bool


class CapabilityErrorEnvelope(_ClosedResult):
    error: CapabilityErrorPayload


class CapabilityFailure(Exception):
    def __init__(
        self,
        code: ContractErrorCode,
        reason_code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        if not isinstance(code, ContractErrorCode):
            raise TypeError("code must be ContractErrorCode")
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 128:
            raise ValueError("reason_code must be a stable non-empty string up to 128 characters")
        if not isinstance(message, str) or len(message) > 256:
            raise ValueError("message must be a string up to 256 characters")
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code
        self.message = message
        self.retryable = retryable


class FsListEntry(_ClosedResult):
    name: str
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None


class FsListResult(_ClosedResult):
    schema_version: Literal[1]
    path: str
    entries: list[FsListEntry]
    truncated: bool
    scanned_entries: int
    skipped_invalid_names: int


class FsSearchResultItem(_ClosedResult):
    path: str
    line_number: int | None
    line_text: str | None
    line_truncated: bool
    file_sha256: FsPatchExpectedSha256 | None


class FsSearchResult(_ClosedResult):
    schema_version: Literal[1]
    results: list[FsSearchResultItem]
    truncated: bool
    limit_reason: Literal["max_files", "max_bytes", "max_results", "max_output", "deadline"] | None
    files_scanned: int
    bytes_scanned: int
    skipped_invalid_utf8: int
    skipped_nul: int
    skipped_symlinks: int


class FsPatchEdit(_ClosedResult):
    old_text: Annotated[StrictStr, Field(min_length=1)]
    new_text: StrictStr


FsPatchEdits = Annotated[list[FsPatchEdit], Field(strict=True, min_length=1, max_length=20)]


class FsPatchResult(_ClosedResult):
    schema_version: Literal[1]
    path: str
    sha256_before: FsPatchExpectedSha256
    sha256_after: FsPatchExpectedSha256
    bytes_before: int
    bytes_after: int
    edits_applied: int


class ReceiptV1Result(_ClosedResult):
    schema_version: Literal[1]
    kind: Literal["repo-diff"]
    digest: FsPatchExpectedSha256


class RepoDiffResult(_ClosedResult):
    schema_version: Literal[1]
    scope: RepoDiffScope
    head_sha: RepoFastForwardSha
    patch: str
    patch_truncated: bool
    full_diff_bytes: int
    diff_receipt: ReceiptV1Result
    network_used: Literal[False]



FsWritePath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsWriteOperation = Literal["create", "replace"]
FsWriteContent = Annotated[StrictStr, Field(max_length=1024 * 1024)]
FsWriteExpectedSha256 = FsPatchExpectedSha256


class FsWriteReceiptResult(_ClosedResult):
    schema_version: Literal[1]
    kind: Literal["fs-write"]
    digest: FsWriteExpectedSha256


class FsWriteResult(_ClosedResult):
    schema_version: Literal[1]
    status: Literal["created", "replaced", "unchanged"]
    path: str
    sha256_before: FsWriteExpectedSha256 | None
    sha256_after: FsWriteExpectedSha256
    bytes_before: int | None
    bytes_after: int
    mode_before: int | None
    mode_after: int
    write_receipt: FsWriteReceiptResult

RepoStageBranch = RepoFastForwardBranch
RepoStageSha = RepoFastForwardSha
RepoStagePath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
RepoStageOperation = Literal["present", "delete"]


class RepoStageItem(_ClosedResult):
    path: RepoStagePath
    operation: RepoStageOperation
    expected_sha256: FsPatchExpectedSha256 | None


RepoStageItems = Annotated[
    list[RepoStageItem],
    Field(strict=True, min_length=1, max_length=50),
]


class RepoStagePathResult(_ClosedResult):
    path: str
    operation: RepoStageOperation
    worktree_sha256: FsPatchExpectedSha256 | None
    git_blob_sha: RepoFastForwardSha | None
    git_mode: Literal["100644", "100755"] | None


class RepoStageResult(_ClosedResult):
    schema_version: Literal[1]
    branch: str
    head_sha: RepoFastForwardSha
    staged_paths: list[RepoStagePathResult]
    staged_diff_receipt: ReceiptV1Result
    post_stage_clean: Literal[True]
    network_used: Literal[False]


def _validate_repo_commit_message(value: str) -> str:
    size = _utf8_size(value, "commit message")
    if size < 1 or size > 16 * 1024:
        raise ValueError("commit message must contain 1..16384 UTF-8 bytes")
    if "\x00" in value:
        raise ValueError("commit message must not contain NUL")
    return value


RepoCommitBranch = RepoFastForwardBranch
RepoCommitSha = RepoFastForwardSha
RepoCommitExpectedDiffReceipt = ReceiptV1Result
RepoCommitMessage = Annotated[
    StrictStr,
    Field(min_length=1, max_length=16 * 1024),
    AfterValidator(_validate_repo_commit_message),
]


class RepoCommitReceiptResult(_ClosedResult):
    schema_version: Literal[1]
    kind: Literal["repo-commit"]
    digest: FsPatchExpectedSha256


class RepoCommitResult(_ClosedResult):
    schema_version: Literal[1]
    branch: str
    parent_sha: RepoFastForwardSha
    tree_sha: RepoFastForwardSha
    commit_sha: RepoFastForwardSha
    diff_receipt: ReceiptV1Result
    commit_receipt: RepoCommitReceiptResult
    network_used: Literal[False]
    post_commit_clean: Literal[True]



CapabilityLifecycle = Literal["stable", "experimental", "deprecated"]
RuntimeVersion = Annotated[StrictStr, Field(min_length=1, max_length=64)]


class CapabilityAuthority(_ClosedResult):
    workspace_bound: bool
    network: Literal["none", "bounded"]
    mutation: Literal["none", "bounded", "destructive"]


class CapabilityAnnotations(_ClosedResult):
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


class CapabilityDescriptor(_ClosedResult):
    schema_version: Literal[1]
    runtime_version: RuntimeVersion
    tool_contract_kernel_version: Literal[1]
    name: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    tool_contract_version: Literal[1]
    lifecycle: CapabilityLifecycle
    authority: CapabilityAuthority
    annotations: CapabilityAnnotations
    request_schema_version: Literal[1]
    result_schema_version: Literal[1] | None
    bounds: dict[str, object]
    supported: bool
    available: bool
    unavailable_reason_code: CapabilityReasonCode | None


class RuntimeCapabilitiesResult(_ClosedResult):
    schema_version: Literal[1]
    runtime_version: RuntimeVersion
    tool_contract_kernel_version: Literal[1]
    capabilities: list[CapabilityDescriptor]


DoctorCheckStatus = Literal["pass", "warn", "fail", "not_applicable"]
DoctorStatus = Literal["healthy", "degraded", "unhealthy"]
DoctorCheckId = Annotated[StrictStr, Field(min_length=1, max_length=64)]
DoctorReasonCode = Annotated[StrictStr, Field(min_length=1, max_length=128)]
DoctorMessage = Annotated[StrictStr, Field(max_length=256)]


class DoctorCheck(_ClosedResult):
    id: DoctorCheckId
    status: DoctorCheckStatus
    reason_code: DoctorReasonCode
    message: DoctorMessage
    evidence: dict[str, object]


class DoctorReport(_ClosedResult):
    schema_version: Literal[1]
    runtime_version: RuntimeVersion
    status: DoctorStatus
    checks: list[DoctorCheck]
