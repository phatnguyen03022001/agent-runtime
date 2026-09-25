from __future__ import annotations

import inspect
import logging
import os
import signal
from functools import wraps
from typing import Any, Callable, cast

try:
    from mcp.server import MCPServer
except ImportError:
    from mcp.server.mcpserver import MCPServer

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict, ValidationError

try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except ImportError:
    _ToolAnnotations = None

from mcp.types import CallToolResult

_MCP_SERVER_LOGGER = logging.getLogger("mcp.server.mcpserver.server")

from .capacity import (
    CAPACITY_OBSERVER_CONTRACT,
    heavy_execution_admission,
    observe_capacity,
)
from .contracts import (
    AbsoluteCwd,
    Argv,
    CapabilityFailure,
    CapacityObserverResult,
    RuntimeCapabilitiesDetail,
    RuntimeCapabilitiesResult,
    RuntimeCapabilityNames,
    ControlAction,
    Cursor,
    ContinuationCursorToken,
    ContinuationReceiptResult,
    PollOutputBytes,
    FsReadBatchResult,
    FsReadItems,
    FsListMaxEntries,
    FsListPath,
    FsListResult,
    FsManageKind,
    FsManageMode,
    FsManageOperation,
    FsManageParents,
    FsManagePath,
    FsManageResult,
    FsPatchEdits,
    FsPatchExpectedSha256,
    FsPatchPath,
    FsPatchResult,
    FsWriteContent,
    FsWriteExpectedSha256,
    FsWriteOperation,
    FsWritePath,
    FsWriteResult,
    FsSearchMaxResults,
    FsSearchMode,
    FsSearchQuery,
    FsSearchResult,
    FsSearchRootPath,
    RepoDiffResult,
    RepoDiffScope,
    RepoStageBranch,
    RepoStageItems,
    RepoStageResult,
    RepoStageSha,
    RepoCommitBranch,
    RepoCommitExpectedDiffReceipt,
    RepoCommitMessage,
    RepoCommitResult,
    RepoCommitSha,
    RepoFastForwardBranch,
    RepoFastForwardResult,
    RepoFastForwardSha,
    RepoPublishBranch,
    RepoPublishResult,
    RepoPublishSha,
    ScreenCaptureBundleId,
    ScreenCaptureCoordinate,
    ScreenCaptureDisplayId,
    ScreenCaptureExtent,
    ScreenCaptureTarget,
    ScreenCaptureWindowId,
    RepoObserverMaxPaths,
    RepoObserverResult,
    RepoRemoteObserverResult,
    SessionId,
    StartIdentity,
    TerminalControlResult,
    TerminalData,
    TerminalDimension,
    TerminalExecResult,
    TerminalMode,
    TerminalPollOutput,
    TerminalSessionResult,
    TERMINAL_POLL_MAX_OUTPUT_BYTES,
    TimeoutSeconds,
    RuntimeToolErrorEnvelope,
    RuntimeToolErrorPayload,
    WaitFor,
    WaitMilliseconds,
)
from .errors import RuntimeStateError, RuntimeValidationError
from .executor import TERMINAL_EXEC_CONTRACT, execute_terminal
from .fs_read import FS_READ_BATCH_CONTRACT, read_files_batch
from .fs_list import FS_LIST_CONTRACT, list_directory
from .fs_manage import FS_MANAGE_CONTRACT, manage_filesystem
from .fs_patch import FS_PATCH_CONTRACT, patch_file
from .fs_write import FS_WRITE_CONTRACT, write_file
from .fs_search import FS_SEARCH_CONTRACT, search_files
from .protection import ProtectedRuntimeDenied
from .repo_diff import REPO_DIFF_CONTRACT, diff_repository
from .repo_stage import REPO_STAGE_CONTRACT, stage_repository
from .repo_commit import REPO_COMMIT_CONTRACT, commit_repository
from .repo_fast_forward import REPO_FAST_FORWARD_CONTRACT, RepoFastForwardFailure, fast_forward_repository
from .repo_observer import REPO_OBSERVER_CONTRACT, RepoObserverFailure, observe_repository
from .repo_remote_observer import REPO_REMOTE_OBSERVER_CONTRACT, RepoRemoteObserverFailure, observe_remote_repository
from .repo_publish import REPO_PUBLISH_CONTRACT, RepoPublishFailure, publish_repository
from .screen_capture import SCREEN_CAPTURE_CONTRACT, ScreenCaptureFailure, capture_screen
from .session import (
    TERMINAL_CONTROL_CONTRACT,
    TERMINAL_POLL_CONTRACT,
    TERMINAL_RESIZE_CONTRACT,
    TERMINAL_START_CONTRACT,
    active_terminal_session_count,
    control_terminal as _control_terminal,
    poll_terminal as _poll_terminal,
    shutdown_terminal_sessions,
    start_terminal as _start_terminal,
)
from .timing import timed_tool_wrapper, timing_middleware
from .capability_registry import (
    ADVERTISED_TOOL_NAMES,
    RUNTIME_CAPABILITIES_CONTRACT,
    runtime_capabilities_result,
)
from .version import RUNTIME_VERSION
from .tool_contract import ContractErrorCode, EffectState, SafeNextAction

PUBLIC_TOOL_NAMES = ADVERTISED_TOOL_NAMES
SERVER_DESCRIPTION = "Bounded local command execution and advisory capacity MCP server; terminal tools may modify the host."
SERVER_INSTRUCTIONS = (
    "Execute literal argv with shell=False and no implicit shell. "
    "Use an absolute cwd under the configured workspace root. "
    "terminal_exec is a caller-keyed synchronous pipe facade; terminal_start begins a PTY or "
    "keyed pipe lifecycle managed with terminal_poll. terminal_control can interrupt or terminate "
    "either mode, pipe input is rejected, and terminal_resize is PTY-only. "
    "Terminal execution uses the operator account's normal permissions "
    "and may modify the host. Protected Runtime filtering is defense-in-depth for recognized argv, shell, "
    "and wrapper lifecycle intent; it is not a sandbox, filesystem confinement, privilege isolation, "
    "syscall filter, or complete prevention of arbitrary same-UID effects. Tools expose bounded output. "
    "capacity_observer provides read-only advisory capacity information. "
    "fs_read_batch performs read-only ordered cwd-relative UTF-8 file reads for at most 20 items "
    "with fixed output and scan-work ceilings and per-item filesystem failures. "
    "fs_list and fs_search provide bounded no-follow filesystem discovery; fs_patch performs "
    "expected-SHA exact text edits, fs_write performs whole-file expected-state create/replace, and "
    "fs_manage performs bounded expected-state mkdir/move/delete/chmod without symlink traversal; "
    "and repo_diff returns bounded local-only "
    "tracked diffs with full-state receipts. "
    "repo_stage stages exactly one complete explicit regular-text candidate with expected-state guards and no implicit git add; "
    "repo_commit commits only the exact receipt-authorized staged state with write-tree, commit-tree, and update-ref compare-and-swap. "
    "repo_observer performs bounded local-only Git repository observation with no fetch, network use, "
    "or repository mutation; it reports local tracking refs, typed changes, diff summary, operation state, "
    "and policy-safe worktree topology. "
    "repo_remote_observer performs bounded read-only fresh fixed-origin branch observation with no fetch or local mutation; "
    "it reports exact origin branch refs only when the complete topology fits the hard limits and leaves ahead/behind unknown. "
    "repo_fast_forward performs expected-state-guarded fixed-origin synchronization from fixed origin only; it fresh-fetches "
    "one bound branch and permits only an exact fast-forward of the current clean branch. "
    "repo_publish performs expected-state-guarded fixed-origin publication of exactly the current clean branch HEAD "
    "when it is the sole direct child of the bound existing origin branch head; repository and task authority remain external. "
    "The stable MCP surface advertises exactly twenty tools. screen_capture remains a known capability descriptor "
    "with VISUAL_PERCEPTION_BLOCKED but is not advertised or callable through MCP; its implementation is retained "
    "for future separately authorized source and activation work, and Screen Recording permission is never requested automatically. "
    "runtime_capabilities returns a compact deterministic summary by default and supports full static descriptor discovery; "
    "it performs no readiness, host-capacity, repository, package, permission, capture, or network probes."
)
class RuntimeMCPServer(MCPServer):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any | None = None,
    ) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            failure = _failure_exception_from_chain(exc)
            if failure is not None:
                return _runtime_error_from_exception(name, failure)
            if _validation_error_in_chain(exc):
                return _runtime_error_result(
                    tool_name=name,
                    code=ContractErrorCode.INVALID_ARGUMENT,
                    reason_code="INVALID_REQUEST_SCHEMA",
                    message="tool arguments failed schema validation",
                    retryable=False,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.FIX_REQUEST,
                )
            if type(exc).__name__ == "UnexpectedToolError":
                _MCP_SERVER_LOGGER.error("unexpected runtime tool failure")
                return _unexpected_error_result(name)
            raise
        except Exception:
            _MCP_SERVER_LOGGER.error("unexpected runtime tool failure")
            return _unexpected_error_result(name)


mcp = RuntimeMCPServer(
    name="Agent Runtime",
    version=RUNTIME_VERSION,
    description=SERVER_DESCRIPTION,
    instructions=SERVER_INSTRUCTIONS,
)

_EXPECTED_TOOL_ERRORS = (
    RuntimeValidationError,
    RuntimeStateError,
    ProtectedRuntimeDenied,
)
_PRESERVED_TOOL_ERRORS = (ToolError, *_EXPECTED_TOOL_ERRORS, CapabilityFailure)
_COMMON_TOOL_SANITIZER_MARKER = "__agent_runtime_common_tool_sanitizer__"

# Exact consequence-bearing boundary inventory for the complete public surface.
# "possible" means an unexpected failure can occur after effect-capable dispatch.
_PUBLIC_TOOL_EFFECT_RISK = {
    "terminal_exec": "possible",
    "terminal_start": "possible",
    "terminal_poll": "absent",
    "terminal_control": "possible",
    "terminal_resize": "possible",
    "capacity_observer": "absent",
    "fs_read_batch": "absent",
    "fs_list": "absent",
    "fs_search": "absent",
    "fs_patch": "possible",
    "fs_write": "possible",
    "fs_manage": "possible",
    "repo_observer": "absent",
    "repo_remote_observer": "absent",
    "repo_diff": "absent",
    "repo_stage": "possible",
    "repo_commit": "possible",
    "repo_fast_forward": "possible",
    "repo_publish": "possible",
    "runtime_capabilities": "absent",
}
if set(_PUBLIC_TOOL_EFFECT_RISK) != set(PUBLIC_TOOL_NAMES):
    raise RuntimeError("public effect-boundary inventory is incomplete")

_TYPED_CODE_TO_CONTRACT = {
    "INVALID_ARGUMENT": ContractErrorCode.INVALID_ARGUMENT,
    "OUTSIDE_WORKSPACE": ContractErrorCode.OUTSIDE_WORKSPACE,
    "NOT_GIT_REPOSITORY": ContractErrorCode.PRECONDITION_FAILED,
    "NOT_REPOSITORY_ROOT": ContractErrorCode.PRECONDITION_FAILED,
    "DIRTY_WORKTREE": ContractErrorCode.PRECONDITION_FAILED,
    "OPERATION_IN_PROGRESS": ContractErrorCode.PRECONDITION_FAILED,
    "DETACHED_HEAD": ContractErrorCode.PRECONDITION_FAILED,
    "BRANCH_MISMATCH": ContractErrorCode.PRECONDITION_FAILED,
    "UPSTREAM_MISMATCH": ContractErrorCode.PRECONDITION_FAILED,
    "LOCAL_HEAD_MISMATCH": ContractErrorCode.PRECONDITION_FAILED,
    "REMOTE_HEAD_MISMATCH": ContractErrorCode.PRECONDITION_FAILED,
    "NON_FAST_FORWARD": ContractErrorCode.PRECONDITION_FAILED,
    "LOCAL_STATE_CHANGED": ContractErrorCode.STATE_CHANGED,
    "FETCH_FAILED": ContractErrorCode.UNAVAILABLE,
    "OUTPUT_LIMIT": ContractErrorCode.LIMIT_EXCEEDED,
    "DEADLINE_EXCEEDED": ContractErrorCode.TIMEOUT,
    "TRANSIENT_FAILURE": ContractErrorCode.UNAVAILABLE,
    "FAST_FORWARD_FAILED": ContractErrorCode.UNAVAILABLE,
    "PUBLICATION_LINEAGE_MISMATCH": ContractErrorCode.PRECONDITION_FAILED,
    "PUSH_FAILED": ContractErrorCode.UNAVAILABLE,
    "PUBLICATION_AMBIGUOUS": ContractErrorCode.UNAVAILABLE,
    "SCREEN_CAPTURE_PERMISSION_REQUIRED": ContractErrorCode.PERMISSION_DENIED,
    "CAPTURE_TARGET_NOT_FOUND": ContractErrorCode.NOT_FOUND,
    "CAPTURE_TARGET_AMBIGUOUS": ContractErrorCode.PRECONDITION_FAILED,
    "CAPTURE_PAYLOAD_TOO_LARGE": ContractErrorCode.LIMIT_EXCEEDED,
    "CAPTURE_PROTOCOL_ERROR": ContractErrorCode.UNAVAILABLE,
    "CAPTURE_HELPER_UNAVAILABLE": ContractErrorCode.UNAVAILABLE,
    "VISUAL_PERCEPTION_BLOCKED": ContractErrorCode.UNAVAILABLE,
    "INTERNAL_ERROR": ContractErrorCode.INTERNAL_ERROR,
}


def _bounded_message(message: object, *, fallback: str = "runtime failure") -> str:
    clean = " ".join(str(message).split())[:256]
    return clean or fallback


def _default_failure_semantics(
    *,
    tool_name: str,
    code: ContractErrorCode,
    reason_code: str,
    retryable: bool,
) -> tuple[bool, EffectState, bool, SafeNextAction]:
    if reason_code == "VISUAL_PERCEPTION_BLOCKED":
        return False, EffectState.ABSENT, False, SafeNextAction.UNSUPPORTED
    if tool_name == "repo_publish" and reason_code == "PUBLICATION_AMBIGUOUS":
        return False, EffectState.UNKNOWN, True, SafeNextAction.RECONCILE
    if tool_name == "repo_publish" and reason_code == "PUSH_FAILED":
        # repo_publish emits PUSH_FAILED only after fresh observation proves the
        # bound remote head remained unchanged after the single push attempt.
        return True, EffectState.ABSENT, False, SafeNextAction.RETRY
    if tool_name == "repo_fast_forward" and reason_code == "FAST_FORWARD_FAILED":
        return False, EffectState.UNKNOWN, True, SafeNextAction.RECONCILE

    if code in {
        ContractErrorCode.INVALID_ARGUMENT,
        ContractErrorCode.OUTSIDE_WORKSPACE,
        ContractErrorCode.PRECONDITION_FAILED,
        ContractErrorCode.STATE_CHANGED,
        ContractErrorCode.CONFLICT,
        ContractErrorCode.PERMISSION_DENIED,
        ContractErrorCode.NOT_FOUND,
    }:
        return False, EffectState.ABSENT, False, SafeNextAction.FIX_REQUEST

    if code is ContractErrorCode.LIMIT_EXCEEDED:
        action = SafeNextAction.WAIT if retryable else SafeNextAction.FIX_REQUEST
        return retryable, EffectState.ABSENT, False, action

    effect_risk = _PUBLIC_TOOL_EFFECT_RISK.get(tool_name, "possible")
    if code in {ContractErrorCode.TIMEOUT, ContractErrorCode.UNAVAILABLE}:
        if effect_risk == "absent":
            action = SafeNextAction.RETRY if retryable else SafeNextAction.REPORT_DEFECT
            return retryable, EffectState.ABSENT, False, action
        return False, EffectState.UNKNOWN, True, SafeNextAction.RECONCILE

    if code is ContractErrorCode.INTERNAL_ERROR:
        if effect_risk == "absent":
            return False, EffectState.ABSENT, False, SafeNextAction.REPORT_DEFECT
        return False, EffectState.UNKNOWN, True, SafeNextAction.RECONCILE

    if effect_risk == "absent":
        return retryable, EffectState.ABSENT, False, (
            SafeNextAction.RETRY if retryable else SafeNextAction.REPORT_DEFECT
        )
    return False, EffectState.UNKNOWN, True, SafeNextAction.RECONCILE


def _runtime_error_result(
    *,
    tool_name: str,
    code: ContractErrorCode,
    reason_code: str,
    message: object,
    retryable: bool,
    effect_state: EffectState | None = None,
    reconciliation_required: bool | None = None,
    safe_next_action: SafeNextAction | None = None,
) -> CallToolResult:
    from mcp.types import TextContent

    explicit = (
        effect_state is not None,
        reconciliation_required is not None,
        safe_next_action is not None,
    )
    if any(explicit) and not all(explicit):
        raise RuntimeError("incomplete internal failure semantics")
    if all(explicit):
        assert effect_state is not None
        assert reconciliation_required is not None
        assert safe_next_action is not None
        resolved = (retryable, effect_state, reconciliation_required, safe_next_action)
    else:
        resolved = _default_failure_semantics(
            tool_name=tool_name,
            code=code,
            reason_code=reason_code,
            retryable=retryable,
        )

    resolved_retryable, resolved_effect, resolved_reconcile, resolved_action = resolved
    clean_message = _bounded_message(message)
    envelope = RuntimeToolErrorEnvelope(
        error=RuntimeToolErrorPayload(
            code=code,
            reason_code=reason_code,
            message=clean_message,
            retryable=resolved_retryable,
            effect_state=resolved_effect.value,
            reconciliation_required=resolved_reconcile,
            safe_next_action=resolved_action.value,
        )
    )
    return CallToolResult(
        content=[TextContent(type="text", text=clean_message)],
        structuredContent=envelope.model_dump(mode="json"),
        isError=True,
    )


def _unexpected_error_result(tool_name: str) -> CallToolResult:
    return _runtime_error_result(
        tool_name=tool_name,
        code=ContractErrorCode.INTERNAL_ERROR,
        reason_code="UNEXPECTED_INTERNAL_ERROR",
        message=f"Error executing tool {tool_name}",
        retryable=False,
    )


def _runtime_error_from_exception(tool_name: str, exc: Exception) -> CallToolResult:
    if isinstance(exc, ProtectedRuntimeDenied):
        return _runtime_error_result(
            tool_name=tool_name,
            code=ContractErrorCode.PERMISSION_DENIED,
            reason_code="PROTECTED_RUNTIME_DENIED",
            message=str(exc),
            retryable=False,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.FIX_REQUEST,
        )

    explicit_contract_code = getattr(exc, "contract_code", None)
    if isinstance(explicit_contract_code, ContractErrorCode):
        code = explicit_contract_code
        reason_code = str(getattr(exc, "reason_code", code.value))
    else:
        raw_code = getattr(exc, "code", ContractErrorCode.INTERNAL_ERROR)
        if isinstance(raw_code, ContractErrorCode):
            code = raw_code
            reason_code = str(getattr(exc, "reason_code", raw_code.value))
        else:
            reason_code = str(raw_code)
            code = _TYPED_CODE_TO_CONTRACT.get(reason_code, ContractErrorCode.INTERNAL_ERROR)

    effect_state = getattr(exc, "effect_state", None)
    reconciliation_required = getattr(exc, "reconciliation_required", None)
    safe_next_action = getattr(exc, "safe_next_action", None)
    return _runtime_error_result(
        tool_name=tool_name,
        code=code,
        reason_code=reason_code,
        message=getattr(exc, "message", str(exc)),
        retryable=bool(getattr(exc, "retryable", False)),
        effect_state=effect_state,
        reconciliation_required=reconciliation_required,
        safe_next_action=safe_next_action,
    )


_FAILURE_SEMANTIC_FIELDS = (
    "reason_code",
    "message",
    "retryable",
    "effect_state",
    "reconciliation_required",
    "safe_next_action",
)


def _has_failure_semantics(exc: BaseException) -> bool:
    return isinstance(exc, ProtectedRuntimeDenied) or (
        (hasattr(exc, "contract_code") or hasattr(exc, "code"))
        and all(hasattr(exc, field) for field in _FAILURE_SEMANTIC_FIELDS)
    )


def _failure_exception_from_chain(exc: BaseException) -> Exception | None:
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, Exception) and _has_failure_semantics(current):
            return current
        current = current.__cause__
    return None


def _validation_error_in_chain(exc: BaseException) -> bool:
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, ValidationError):
            return True
        current = current.__cause__
    return False


def _call_runtime_tool(_tool_name: str, delegate: Callable[..., Any], *args: Any) -> Any:
    return delegate(*args)


def _sanitize_unexpected_tool_exception(delegate: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(delegate)
    def sanitized(*args: Any, **kwargs: Any) -> Any:
        try:
            return delegate(*args, **kwargs)
        except _PRESERVED_TOOL_ERRORS:
            raise
        except Exception as exc:
            if _has_failure_semantics(exc):
                raise
            raise RuntimeError("unexpected runtime tool failure") from None

    setattr(sanitized, _COMMON_TOOL_SANITIZER_MARKER, True)
    return sanitized


def _capability_error_result(tool_name: str, exc: CapabilityFailure) -> CallToolResult:
    return _runtime_error_from_exception(tool_name, exc)


def _tool_annotations_supported() -> bool:
    if _ToolAnnotations is None:
        return False
    try:
        signature = inspect.signature(mcp.tool)
    except (TypeError, ValueError):
        return False
    return "annotations" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _build_tool_annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> Any | None:
    if _ToolAnnotations is None or not _tool_annotations_supported():
        return None

    field_names: set[str] = set()
    for attribute in ("model_fields", "__fields__", "__annotations__"):
        fields = getattr(_ToolAnnotations, attribute, None)
        if isinstance(fields, dict):
            field_names.update(str(name) for name in fields)

    camel = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    snake = {"read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"}
    if camel.issubset(field_names):
        kwargs = {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        }
    elif snake.issubset(field_names):
        kwargs = {
            "read_only_hint": read_only,
            "destructive_hint": destructive,
            "idempotent_hint": idempotent,
            "open_world_hint": open_world,
        }
    else:
        try:
            signature = inspect.signature(_ToolAnnotations)
        except (TypeError, ValueError):
            return None
        names = set(signature.parameters)
        if camel.issubset(names):
            kwargs = {
                "readOnlyHint": read_only,
                "destructiveHint": destructive,
                "idempotentHint": idempotent,
                "openWorldHint": open_world,
            }
        elif snake.issubset(names):
            kwargs = {
                "read_only_hint": read_only,
                "destructive_hint": destructive,
                "idempotent_hint": idempotent,
                "open_world_hint": open_world,
            }
        else:
            return None

    try:
        return _ToolAnnotations(**kwargs)
    except (TypeError, ValueError):
        return None


def _close_registered_tool_input(tool_name: str) -> None:
    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is None:
        return
    registered = tool_manager.get_tool(tool_name)
    if registered is None:
        raise RuntimeError(f"registered tool not found: {tool_name}")
    argument_model = registered.fn_metadata.arg_model
    model_config = dict(argument_model.model_config)
    model_config["extra"] = "forbid"
    argument_model.model_config = ConfigDict(**model_config)
    argument_model.model_rebuild(force=True)
    registered.parameters = argument_model.model_json_schema(by_alias=True)


def _tool(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    annotations = _build_tool_annotations(
        read_only=read_only,
        destructive=destructive,
        idempotent=idempotent,
        open_world=open_world,
    )

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        sanitized_function = _sanitize_unexpected_tool_exception(function)
        observed_function = timed_tool_wrapper(function.__name__, sanitized_function)
        registered_function: Callable[..., Any]
        if annotations is not None:
            try:
                registered_function = mcp.tool(annotations=annotations)(observed_function)
            except (TypeError, ValueError):
                registered_function = mcp.tool()(observed_function)
        else:
            registered_function = mcp.tool()(observed_function)
        _close_registered_tool_input(observed_function.__name__)
        return registered_function

    return decorator


@_tool(
    read_only=TERMINAL_EXEC_CONTRACT.annotations.read_only,
    destructive=TERMINAL_EXEC_CONTRACT.annotations.destructive,
    idempotent=TERMINAL_EXEC_CONTRACT.annotations.idempotent,
    open_world=TERMINAL_EXEC_CONTRACT.annotations.open_world,
)
def terminal_exec(
    argv: Argv,
    cwd: AbsoluteCwd,
    start_identity: StartIdentity,
    timeout_seconds: TimeoutSeconds = 300.0,
) -> TerminalExecResult:
    """Run or join keyed local argv through shared pipes; the process may modify the host."""

    return cast(
        TerminalExecResult,
        _call_runtime_tool(
            "terminal_exec", execute_terminal, argv, cwd, timeout_seconds, start_identity
        ),
    )


@_tool(
    read_only=TERMINAL_START_CONTRACT.annotations.read_only,
    destructive=TERMINAL_START_CONTRACT.annotations.destructive,
    idempotent=TERMINAL_START_CONTRACT.annotations.idempotent,
    open_world=TERMINAL_START_CONTRACT.annotations.open_world,
)
def terminal_start(
    argv: Argv,
    cwd: AbsoluteCwd,
    start_identity: StartIdentity | None = None,
    mode: TerminalMode = "pty",
) -> TerminalSessionResult:
    """Start a keyed PTY or pipe process; it may modify the host."""

    return cast(
        TerminalSessionResult,
        _call_runtime_tool("terminal_start", _start_terminal, argv, cwd, start_identity, mode),
    )


@_tool(
    read_only=TERMINAL_POLL_CONTRACT.annotations.read_only,
    destructive=TERMINAL_POLL_CONTRACT.annotations.destructive,
    idempotent=TERMINAL_POLL_CONTRACT.annotations.idempotent,
    open_world=TERMINAL_POLL_CONTRACT.annotations.open_world,
)
def terminal_poll(
    session_id: SessionId | None = None,
    start_identity: StartIdentity | None = None,
    cursor: Cursor = 0,
    wait_ms: WaitMilliseconds = 0,
    wait_for: WaitFor = "output_or_state",
    output: TerminalPollOutput = "incremental",
    max_output_bytes: PollOutputBytes = TERMINAL_POLL_MAX_OUTPUT_BYTES,
) -> TerminalSessionResult:
    """Read bounded incremental output or status without consuming output."""

    return cast(
        TerminalSessionResult,
        _call_runtime_tool(
            "terminal_poll",
            _poll_terminal,
            session_id,
            cursor,
            wait_ms,
            start_identity,
            wait_for,
            output,
            max_output_bytes,
        ),
    )


@_tool(
    read_only=TERMINAL_CONTROL_CONTRACT.annotations.read_only,
    destructive=TERMINAL_CONTROL_CONTRACT.annotations.destructive,
    idempotent=TERMINAL_CONTROL_CONTRACT.annotations.idempotent,
    open_world=TERMINAL_CONTROL_CONTRACT.annotations.open_world,
)
def terminal_control(
    session_id: SessionId,
    action: ControlAction,
    data: TerminalData = None,
) -> TerminalControlResult:
    """Write to a PTY or interrupt/terminate a process that may modify the host."""

    return cast(
        TerminalControlResult,
        _call_runtime_tool("terminal_control", _control_terminal, session_id, action, data, None, None),
    )


@_tool(
    read_only=TERMINAL_RESIZE_CONTRACT.annotations.read_only,
    destructive=TERMINAL_RESIZE_CONTRACT.annotations.destructive,
    idempotent=TERMINAL_RESIZE_CONTRACT.annotations.idempotent,
    open_world=TERMINAL_RESIZE_CONTRACT.annotations.open_world,
)
def terminal_resize(
    session_id: SessionId,
    rows: TerminalDimension,
    cols: TerminalDimension,
) -> TerminalControlResult:
    """Resize one persistent PTY session using bounded terminal dimensions."""

    return cast(
        TerminalControlResult,
        _call_runtime_tool("terminal_resize", _control_terminal, session_id, "resize", None, rows, cols),
    )


def _operational_capacity_snapshot() -> tuple[int, int, int]:
    admission = heavy_execution_admission()
    return (
        admission.active,
        admission.limit,
        active_terminal_session_count(),
    )


@_tool(
    read_only=CAPACITY_OBSERVER_CONTRACT.annotations.read_only,
    destructive=CAPACITY_OBSERVER_CONTRACT.annotations.destructive,
    idempotent=CAPACITY_OBSERVER_CONTRACT.annotations.idempotent,
    open_world=CAPACITY_OBSERVER_CONTRACT.annotations.open_world,
)
def capacity_observer() -> CapacityObserverResult:
    """Report advisory host capacity plus point-in-time Runtime usage."""

    return cast(
        CapacityObserverResult,
        _call_runtime_tool(
            "capacity_observer",
            observe_capacity,
            _operational_capacity_snapshot,
        ),
    )


@_tool(
    read_only=FS_READ_BATCH_CONTRACT.annotations.read_only,
    destructive=FS_READ_BATCH_CONTRACT.annotations.destructive,
    idempotent=FS_READ_BATCH_CONTRACT.annotations.idempotent,
    open_world=FS_READ_BATCH_CONTRACT.annotations.open_world,
)
def fs_read_batch(cwd: AbsoluteCwd, items: FsReadItems) -> FsReadBatchResult:
    """Read bounded ordered UTF-8 file ranges below one validated cwd."""

    return cast(FsReadBatchResult, _call_runtime_tool("fs_read_batch", read_files_batch, cwd, items))


@_tool(
    read_only=FS_LIST_CONTRACT.annotations.read_only,
    destructive=FS_LIST_CONTRACT.annotations.destructive,
    idempotent=FS_LIST_CONTRACT.annotations.idempotent,
    open_world=FS_LIST_CONTRACT.annotations.open_world,
)
def fs_list(
    cwd: AbsoluteCwd,
    path: FsListPath = ".",
    max_entries: FsListMaxEntries = 200,
    cursor: ContinuationCursorToken | None = None,
    continuation_receipt: ContinuationReceiptResult | None = None,
) -> FsListResult:
    """List one directory non-recursively with deterministic no-follow metadata."""

    try:
        return list_directory(cwd, path, max_entries, cursor, continuation_receipt)
    except CapabilityFailure as exc:
        return cast(FsListResult, _capability_error_result("fs_list", exc))


@_tool(
    read_only=FS_SEARCH_CONTRACT.annotations.read_only,
    destructive=FS_SEARCH_CONTRACT.annotations.destructive,
    idempotent=FS_SEARCH_CONTRACT.annotations.idempotent,
    open_world=FS_SEARCH_CONTRACT.annotations.open_world,
)
def fs_search(
    cwd: AbsoluteCwd,
    query: FsSearchQuery,
    mode: FsSearchMode,
    root_path: FsSearchRootPath = ".",
    case_sensitive: bool = True,
    max_results: FsSearchMaxResults = 100,
    cursor: ContinuationCursorToken | None = None,
    continuation_receipt: ContinuationReceiptResult | None = None,
) -> FsSearchResult:
    """Search regular files with literal bounded deterministic semantics."""

    try:
        return search_files(
            cwd,
            query,
            mode,
            root_path,
            case_sensitive,
            max_results,
            cursor,
            continuation_receipt,
        )
    except CapabilityFailure as exc:
        return cast(FsSearchResult, _capability_error_result("fs_search", exc))


@_tool(
    read_only=FS_PATCH_CONTRACT.annotations.read_only,
    destructive=FS_PATCH_CONTRACT.annotations.destructive,
    idempotent=FS_PATCH_CONTRACT.annotations.idempotent,
    open_world=FS_PATCH_CONTRACT.annotations.open_world,
)
def fs_patch(
    cwd: AbsoluteCwd,
    path: FsPatchPath,
    expected_sha256: FsPatchExpectedSha256,
    edits: FsPatchEdits,
) -> FsPatchResult:
    """Apply exact UTF-8 text edits under expected-state and atomic-write guards."""

    try:
        return patch_file(cwd, path, expected_sha256, edits)
    except CapabilityFailure as exc:
        return cast(FsPatchResult, _capability_error_result("fs_patch", exc))


@_tool(
    read_only=FS_WRITE_CONTRACT.annotations.read_only,
    destructive=FS_WRITE_CONTRACT.annotations.destructive,
    idempotent=FS_WRITE_CONTRACT.annotations.idempotent,
    open_world=FS_WRITE_CONTRACT.annotations.open_world,
)
def fs_write(
    cwd: AbsoluteCwd,
    path: FsWritePath,
    operation: FsWriteOperation,
    content: FsWriteContent,
    expected_sha256: FsWriteExpectedSha256 | None = None,
) -> FsWriteResult:
    """Create or replace one whole UTF-8 file under explicit expected-state guards."""

    try:
        return write_file(cwd, path, operation, content, expected_sha256)
    except CapabilityFailure as exc:
        return cast(FsWriteResult, _capability_error_result("fs_write", exc))


@_tool(
    read_only=FS_MANAGE_CONTRACT.annotations.read_only,
    destructive=FS_MANAGE_CONTRACT.annotations.destructive,
    idempotent=FS_MANAGE_CONTRACT.annotations.idempotent,
    open_world=FS_MANAGE_CONTRACT.annotations.open_world,
)
def fs_manage(
    cwd: AbsoluteCwd,
    operation: FsManageOperation,
    path: FsManagePath | None = None,
    source_path: FsManagePath | None = None,
    destination_path: FsManagePath | None = None,
    expected_kind: FsManageKind | None = None,
    expected_sha256: FsPatchExpectedSha256 | None = None,
    expected_source_sha256: FsPatchExpectedSha256 | None = None,
    parents: FsManageParents = False,
    mode: FsManageMode | None = None,
) -> FsManageResult:
    """Apply one bounded guarded local filesystem management operation."""

    try:
        return manage_filesystem(
            cwd,
            operation,
            path,
            source_path,
            destination_path,
            expected_kind,
            expected_sha256,
            expected_source_sha256,
            parents,
            mode,
        )
    except CapabilityFailure as exc:
        return cast(FsManageResult, _capability_error_result("fs_manage", exc))


@_tool(
    read_only=REPO_OBSERVER_CONTRACT.annotations.read_only,
    destructive=REPO_OBSERVER_CONTRACT.annotations.destructive,
    idempotent=REPO_OBSERVER_CONTRACT.annotations.idempotent,
    open_world=REPO_OBSERVER_CONTRACT.annotations.open_world,
)
def repo_observer(
    cwd: AbsoluteCwd,
    max_paths: RepoObserverMaxPaths = 200,
    cursor: ContinuationCursorToken | None = None,
    continuation_receipt: ContinuationReceiptResult | None = None,
) -> RepoObserverResult:
    """Observe one local-only Git working tree without network access or mutation."""

    try:
        return observe_repository(cwd, max_paths, cursor, continuation_receipt)
    except RepoObserverFailure as exc:
        return cast(RepoObserverResult, _runtime_error_from_exception("repo_observer", exc))


@_tool(
    read_only=REPO_REMOTE_OBSERVER_CONTRACT.annotations.read_only,
    destructive=REPO_REMOTE_OBSERVER_CONTRACT.annotations.destructive,
    idempotent=REPO_REMOTE_OBSERVER_CONTRACT.annotations.idempotent,
    open_world=REPO_REMOTE_OBSERVER_CONTRACT.annotations.open_world,
)
def repo_remote_observer(
    cwd: AbsoluteCwd,
) -> RepoRemoteObserverResult:
    """Observe fresh fixed-origin branch refs without fetch or local mutation."""

    try:
        return observe_remote_repository(cwd)
    except RepoRemoteObserverFailure as exc:
        return cast(RepoRemoteObserverResult, _runtime_error_from_exception("repo_remote_observer", exc))


@_tool(
    read_only=REPO_DIFF_CONTRACT.annotations.read_only,
    destructive=REPO_DIFF_CONTRACT.annotations.destructive,
    idempotent=REPO_DIFF_CONTRACT.annotations.idempotent,
    open_world=REPO_DIFF_CONTRACT.annotations.open_world,
)
def repo_diff(
    cwd: AbsoluteCwd,
    scope: RepoDiffScope = "worktree",
    cursor: ContinuationCursorToken | None = None,
    continuation_receipt: ContinuationReceiptResult | None = None,
) -> RepoDiffResult:
    """Return one bounded local tracked diff with a full-state receipt."""

    try:
        return diff_repository(cwd, scope, cursor, continuation_receipt)
    except CapabilityFailure as exc:
        return cast(RepoDiffResult, _capability_error_result("repo_diff", exc))


@_tool(
    read_only=REPO_STAGE_CONTRACT.annotations.read_only,
    destructive=REPO_STAGE_CONTRACT.annotations.destructive,
    idempotent=REPO_STAGE_CONTRACT.annotations.idempotent,
    open_world=REPO_STAGE_CONTRACT.annotations.open_world,
)
def repo_stage(
    cwd: AbsoluteCwd,
    branch: RepoStageBranch,
    expected_head_sha: RepoStageSha,
    items: RepoStageItems,
) -> RepoStageResult:
    """Stage one complete explicit regular-text repository candidate."""

    try:
        return stage_repository(cwd, branch, expected_head_sha, items)
    except CapabilityFailure as exc:
        return cast(RepoStageResult, _capability_error_result("repo_stage", exc))


@_tool(
    read_only=REPO_COMMIT_CONTRACT.annotations.read_only,
    destructive=REPO_COMMIT_CONTRACT.annotations.destructive,
    idempotent=REPO_COMMIT_CONTRACT.annotations.idempotent,
    open_world=REPO_COMMIT_CONTRACT.annotations.open_world,
)
def repo_commit(
    cwd: AbsoluteCwd,
    branch: RepoCommitBranch,
    expected_head_sha: RepoCommitSha,
    expected_diff_receipt: RepoCommitExpectedDiffReceipt,
    message: RepoCommitMessage,
) -> RepoCommitResult:
    """Commit exactly one receipt-authorized staged state using Git plumbing."""

    try:
        return commit_repository(
            cwd,
            branch,
            expected_head_sha,
            expected_diff_receipt,
            message,
        )
    except CapabilityFailure as exc:
        return cast(RepoCommitResult, _capability_error_result("repo_commit", exc))


@_tool(
    read_only=REPO_FAST_FORWARD_CONTRACT.annotations.read_only,
    destructive=REPO_FAST_FORWARD_CONTRACT.annotations.destructive,
    idempotent=REPO_FAST_FORWARD_CONTRACT.annotations.idempotent,
    open_world=REPO_FAST_FORWARD_CONTRACT.annotations.open_world,
)
def repo_fast_forward(
    cwd: AbsoluteCwd,
    branch: RepoFastForwardBranch,
    expected_local_head: RepoFastForwardSha,
    expected_remote_head: RepoFastForwardSha,
) -> RepoFastForwardResult:
    """Fast-forward one clean bound branch after a fresh fixed-origin verification."""

    try:
        return fast_forward_repository(
            cwd,
            branch,
            expected_local_head,
            expected_remote_head,
        )
    except RepoFastForwardFailure as exc:
        return cast(
            RepoFastForwardResult,
            _runtime_error_from_exception("repo_fast_forward", exc),
        )


@_tool(
    read_only=REPO_PUBLISH_CONTRACT.annotations.read_only,
    destructive=REPO_PUBLISH_CONTRACT.annotations.destructive,
    idempotent=REPO_PUBLISH_CONTRACT.annotations.idempotent,
    open_world=REPO_PUBLISH_CONTRACT.annotations.open_world,
)
def repo_publish(
    cwd: AbsoluteCwd,
    branch: RepoPublishBranch,
    expected_remote_head: RepoPublishSha,
    commit: RepoPublishSha,
) -> RepoPublishResult:
    """Publish one clean direct-child commit to its existing fixed origin branch."""

    try:
        return publish_repository(cwd, branch, expected_remote_head, commit)
    except RepoPublishFailure as exc:
        return cast(
            RepoPublishResult,
            _runtime_error_from_exception("repo_publish", exc),
        )


def screen_capture(
    target: ScreenCaptureTarget = "frontmost_window",
    window_id: ScreenCaptureWindowId | None = None,
    application_bundle_id: ScreenCaptureBundleId | None = None,
    display_id: ScreenCaptureDisplayId | None = None,
    x: ScreenCaptureCoordinate | None = None,
    y: ScreenCaptureCoordinate | None = None,
    width: ScreenCaptureExtent | None = None,
    height: ScreenCaptureExtent | None = None,
) -> CallToolResult:
    """Return a production governance denial before capture_screen or the native helper can execute."""

    return _runtime_error_from_exception(
        "screen_capture",
        ScreenCaptureFailure(
            "VISUAL_PERCEPTION_BLOCKED",
            "visual perception is governance-blocked in production",
            retryable=False,
        ),
    )


@_tool(
    read_only=RUNTIME_CAPABILITIES_CONTRACT.annotations.read_only,
    destructive=RUNTIME_CAPABILITIES_CONTRACT.annotations.destructive,
    idempotent=RUNTIME_CAPABILITIES_CONTRACT.annotations.idempotent,
    open_world=RUNTIME_CAPABILITIES_CONTRACT.annotations.open_world,
)
def runtime_capabilities(
    detail: RuntimeCapabilitiesDetail = "summary",
    names: RuntimeCapabilityNames | None = None,
) -> RuntimeCapabilitiesResult:
    """Return compact summary or exact static capability descriptors."""

    return runtime_capabilities_result(detail=detail, names=names)


def _install_timing_middleware() -> None:
    append = getattr(getattr(mcp, "middleware", None), "append", None)
    if append is not None:
        append(timing_middleware)


_install_timing_middleware()


def _handle_termination_signal(signum: int, _frame: Any) -> None:
    """Clean up Runtime-owned terminal roots before default signal exit."""

    try:
        shutdown_terminal_sessions()
    finally:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _install_termination_handlers() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _handle_termination_signal)


def _main() -> int:
    _install_termination_handlers()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
