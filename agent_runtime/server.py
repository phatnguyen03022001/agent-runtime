from __future__ import annotations

import inspect
import os
import signal
from typing import Any, Callable, cast

try:
    from mcp.server import MCPServer
except ImportError:
    from mcp.server.mcpserver import MCPServer

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict

try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except ImportError:
    _ToolAnnotations = None

from mcp.types import CallToolResult

from .capacity import observe_capacity
from .contracts import (
    AbsoluteCwd,
    Argv,
    CapabilityErrorEnvelope,
    CapabilityErrorPayload,
    CapabilityFailure,
    CapacityObserverResult,
    ControlAction,
    Cursor,
    FsReadBatchResult,
    FsReadItems,
    FsListMaxEntries,
    FsListPath,
    FsListResult,
    FsPatchEdits,
    FsPatchExpectedSha256,
    FsPatchPath,
    FsPatchResult,
    FsSearchMaxResults,
    FsSearchMode,
    FsSearchQuery,
    FsSearchResult,
    FsSearchRootPath,
    RepoDiffResult,
    RepoDiffScope,
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
    SessionId,
    TerminalControlResult,
    TerminalData,
    TerminalDimension,
    TerminalExecResult,
    TerminalSessionResult,
    TimeoutSeconds,
    TypedToolErrorEnvelope,
    TypedToolErrorPayload,
    WaitMilliseconds,
)
from .errors import RuntimeStateError, RuntimeValidationError
from .executor import execute_terminal, shutdown_terminal_executions
from .fs_read import FS_READ_BATCH_CONTRACT, read_files_batch
from .fs_list import FS_LIST_CONTRACT, list_directory
from .fs_patch import FS_PATCH_CONTRACT, patch_file
from .fs_search import FS_SEARCH_CONTRACT, search_files
from .protection import ProtectedRuntimeDenied
from .repo_diff import REPO_DIFF_CONTRACT, diff_repository
from .repo_fast_forward import RepoFastForwardFailure, fast_forward_repository
from .repo_observer import RepoObserverFailure, observe_repository
from .repo_publish import RepoPublishFailure, publish_repository
from .screen_capture import ScreenCaptureFailure, capture_failure_result, capture_screen
from .session import (
    control_terminal as _control_terminal,
    poll_terminal as _poll_terminal,
    shutdown_terminal_sessions,
    start_terminal as _start_terminal,
)
from .timing import timed_tool_wrapper, timing_middleware

PUBLIC_TOOL_NAMES = (
    "terminal_exec",
    "terminal_start",
    "terminal_poll",
    "terminal_control",
    "terminal_resize",
    "capacity_observer",
    "fs_read_batch",
    "fs_list",
    "fs_search",
    "fs_patch",
    "repo_observer",
    "repo_diff",
    "repo_fast_forward",
    "repo_publish",
    "screen_capture",
)
SERVER_DESCRIPTION = "Bounded local command execution and advisory capacity MCP server; terminal tools may modify the host."
SERVER_INSTRUCTIONS = (
    "Execute literal argv with shell=False and no implicit shell. "
    "Use an absolute cwd under the configured workspace root. "
    "terminal_exec is one-shot; terminal_start begins a PTY lifecycle managed with "
    "terminal_poll, destructive terminal_control, and bounded non-destructive terminal_resize. "
    "Terminal execution uses the operator account's normal permissions "
    "and may modify the host. Protected Runtime filtering is defense-in-depth for recognized argv, shell, "
    "and wrapper lifecycle intent; it is not a sandbox, filesystem confinement, privilege isolation, "
    "syscall filter, or complete prevention of arbitrary same-UID effects. Tools expose bounded output. "
    "capacity_observer provides read-only advisory capacity information. "
    "fs_read_batch performs read-only ordered cwd-relative UTF-8 file reads for at most 20 items "
    "with fixed output and scan-work ceilings and per-item filesystem failures. "
    "fs_list and fs_search provide bounded no-follow filesystem discovery; fs_patch performs "
    "expected-SHA exact text edits with atomic replacement, and repo_diff returns bounded local-only "
    "tracked diffs with full-state receipts. "
    "repo_observer performs bounded local-only Git repository observation with no fetch, network use, "
    "or repository mutation; it reports local tracking refs, typed changes, diff summary, operation state, "
    "and policy-safe worktree topology. "
    "repo_fast_forward performs expected-state-guarded fixed-origin synchronization from fixed origin only; it fresh-fetches "
    "one bound branch and permits only an exact fast-forward of the current clean branch. "
    "repo_publish performs expected-state-guarded fixed-origin publication of exactly the current clean branch HEAD "
    "when it is the sole direct child of the bound existing origin branch head; repository and task authority remain external. "
    "screen_capture remains exposed for contract compatibility, but visual perception is governance-blocked in "
    "production: every invocation returns typed VISUAL_PERCEPTION_BLOCKED before native capture and produces no "
    "image; only future Architect re-authorization plus new source verification, packaging, and activation may "
    "change that state; Screen Recording permission is never requested automatically."
)
mcp = MCPServer(
    name="Agent Runtime",
    version="0.2.0",
    description=SERVER_DESCRIPTION,
    instructions=SERVER_INSTRUCTIONS,
)

_EXPECTED_TOOL_ERRORS = (
    RuntimeValidationError,
    RuntimeStateError,
    ProtectedRuntimeDenied,
)


def _call_runtime_tool(delegate: Callable[..., Any], *args: Any) -> Any:
    try:
        return delegate(*args)
    except _EXPECTED_TOOL_ERRORS as exc:
        raise ToolError(str(exc)) from None


def _capability_error_result(exc: CapabilityFailure) -> CallToolResult:
    from mcp.types import TextContent

    envelope = CapabilityErrorEnvelope(
        error=CapabilityErrorPayload(
            code=exc.code,
            reason_code=exc.reason_code,
            message=exc.message,
            retryable=exc.retryable,
        )
    )
    return CallToolResult(
        content=[TextContent(type="text", text=exc.message)],
        structuredContent=envelope.model_dump(),
        isError=True,
    )


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
        observed_function = timed_tool_wrapper(function.__name__, function)
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


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
def terminal_exec(
    argv: Argv,
    cwd: AbsoluteCwd,
    timeout_seconds: TimeoutSeconds = 300.0,
) -> TerminalExecResult:
    """Run one literal local argv; this capability may modify the host."""

    return cast(TerminalExecResult, _call_runtime_tool(execute_terminal, argv, cwd, timeout_seconds))


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
def terminal_start(argv: Argv, cwd: AbsoluteCwd) -> TerminalSessionResult:
    """Start one literal argv in a bounded persistent PTY session; the process may modify the host."""

    return cast(TerminalSessionResult, _call_runtime_tool(_start_terminal, argv, cwd))


@_tool(read_only=False, destructive=False, idempotent=False, open_world=False)
def terminal_poll(
    session_id: SessionId,
    cursor: Cursor = 0,
    wait_ms: WaitMilliseconds = 0,
) -> TerminalSessionResult:
    """Read bounded incremental PTY output and current session status."""

    return cast(TerminalSessionResult, _call_runtime_tool(_poll_terminal, session_id, cursor, wait_ms))


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
def terminal_control(
    session_id: SessionId,
    action: ControlAction,
    data: TerminalData = None,
) -> TerminalControlResult:
    """Write, interrupt, or terminate one persistent PTY session whose process may modify the host."""

    return cast(
        TerminalControlResult,
        _call_runtime_tool(_control_terminal, session_id, action, data, None, None),
    )


@_tool(read_only=False, destructive=False, idempotent=True, open_world=False)
def terminal_resize(
    session_id: SessionId,
    rows: TerminalDimension,
    cols: TerminalDimension,
) -> TerminalControlResult:
    """Resize one persistent PTY session using bounded terminal dimensions."""

    return cast(
        TerminalControlResult,
        _call_runtime_tool(_control_terminal, session_id, "resize", None, rows, cols),
    )


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def capacity_observer() -> CapacityObserverResult:
    """Report a bounded read-only advisory machine-capacity ceiling."""

    return cast(CapacityObserverResult, _call_runtime_tool(observe_capacity))


@_tool(
    read_only=FS_READ_BATCH_CONTRACT.annotations.read_only,
    destructive=FS_READ_BATCH_CONTRACT.annotations.destructive,
    idempotent=FS_READ_BATCH_CONTRACT.annotations.idempotent,
    open_world=FS_READ_BATCH_CONTRACT.annotations.open_world,
)
def fs_read_batch(cwd: AbsoluteCwd, items: FsReadItems) -> FsReadBatchResult:
    """Read bounded ordered UTF-8 file ranges below one validated cwd."""

    return cast(FsReadBatchResult, _call_runtime_tool(read_files_batch, cwd, items))


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
) -> FsListResult:
    """List one directory non-recursively with deterministic no-follow metadata."""

    try:
        return list_directory(cwd, path, max_entries)
    except CapabilityFailure as exc:
        return cast(FsListResult, _capability_error_result(exc))


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
) -> FsSearchResult:
    """Search regular files with literal bounded deterministic semantics."""

    try:
        return search_files(cwd, query, mode, root_path, case_sensitive, max_results)
    except CapabilityFailure as exc:
        return cast(FsSearchResult, _capability_error_result(exc))


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
        return cast(FsPatchResult, _capability_error_result(exc))


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def repo_observer(
    cwd: AbsoluteCwd,
    max_paths: RepoObserverMaxPaths = 200,
) -> RepoObserverResult:
    """Observe one local-only Git working tree without network access or mutation."""

    try:
        return observe_repository(cwd, max_paths)
    except RepoObserverFailure as exc:
        from mcp.types import CallToolResult, TextContent

        envelope = TypedToolErrorEnvelope(
            error=TypedToolErrorPayload(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            )
        )
        error_result = CallToolResult(
            content=[TextContent(type="text", text=exc.message)],
            structuredContent=envelope.model_dump(),
            isError=True,
        )
        return cast(RepoObserverResult, error_result)


@_tool(
    read_only=REPO_DIFF_CONTRACT.annotations.read_only,
    destructive=REPO_DIFF_CONTRACT.annotations.destructive,
    idempotent=REPO_DIFF_CONTRACT.annotations.idempotent,
    open_world=REPO_DIFF_CONTRACT.annotations.open_world,
)
def repo_diff(
    cwd: AbsoluteCwd,
    scope: RepoDiffScope = "worktree",
) -> RepoDiffResult:
    """Return one bounded local tracked diff with a full-state receipt."""

    try:
        return diff_repository(cwd, scope)
    except CapabilityFailure as exc:
        return cast(RepoDiffResult, _capability_error_result(exc))


@_tool(read_only=False, destructive=True, idempotent=True, open_world=True)
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
        from mcp.types import CallToolResult, TextContent

        envelope = TypedToolErrorEnvelope(
            error=TypedToolErrorPayload(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            )
        )
        error_result = CallToolResult(
            content=[TextContent(type="text", text=exc.message)],
            structuredContent=envelope.model_dump(),
            isError=True,
        )
        return cast(RepoFastForwardResult, error_result)


@_tool(read_only=False, destructive=True, idempotent=True, open_world=True)
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
        from mcp.types import CallToolResult, TextContent

        envelope = TypedToolErrorEnvelope(
            error=TypedToolErrorPayload(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            )
        )
        error_result = CallToolResult(
            content=[TextContent(type="text", text=exc.message)],
            structuredContent=envelope.model_dump(),
            isError=True,
        )
        return cast(RepoPublishResult, error_result)


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
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

    return capture_failure_result(
        ScreenCaptureFailure(
            "VISUAL_PERCEPTION_BLOCKED",
            "visual perception is governance-blocked in production",
            retryable=False,
        )
    )


def _install_timing_middleware() -> None:
    append = getattr(getattr(mcp, "middleware", None), "append", None)
    if append is not None:
        append(timing_middleware)


_install_timing_middleware()


def _handle_termination_signal(signum: int, _frame: Any) -> None:
    """Clean up Runtime-owned terminal roots before default signal exit."""

    try:
        shutdown_terminal_executions()
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
