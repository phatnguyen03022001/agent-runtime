from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictStr,
    model_serializer,
)

Argv = Annotated[list[StrictStr], Field(strict=True, min_length=1)]
AbsoluteCwd = Annotated[
    StrictStr,
    Field(min_length=1, pattern=r"^/"),
]
TimeoutSeconds = Annotated[float, Field(strict=True, gt=0, le=3600)]
SessionId = Annotated[StrictStr, Field(min_length=1)]
Cursor = Annotated[int, Field(strict=True, ge=0)]
WaitMilliseconds = Annotated[int, Field(strict=True, ge=0, le=1000)]
ControlAction = Literal["write", "interrupt", "terminate", "resize"]
TerminalData = StrictStr | None
TerminalDimension = Annotated[int, Field(strict=True, ge=1, le=65535)]
FsReadPath = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
FsReadLine = Annotated[int, Field(strict=True, ge=1)]
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
