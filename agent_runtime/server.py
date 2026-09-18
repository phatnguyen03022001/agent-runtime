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

from .capacity import observe_capacity
from .contracts import (
    AbsoluteCwd,
    Argv,
    CapacityObserverResult,
    ControlAction,
    Cursor,
    FsReadBatchResult,
    FsReadItems,
    SessionId,
    TerminalControlResult,
    TerminalData,
    TerminalDimension,
    TerminalExecResult,
    TerminalSessionResult,
    TimeoutSeconds,
    WaitMilliseconds,
)
from .errors import RuntimeStateError, RuntimeValidationError
from .executor import execute_terminal, shutdown_terminal_executions
from .fs_read import read_files_batch
from .protection import ProtectedRuntimeDenied
from .session import (
    control_terminal as _control_terminal,
    poll_terminal as _poll_terminal,
    shutdown_terminal_sessions,
    start_terminal as _start_terminal,
)
from .timing import timed_tool_wrapper, timing_middleware

PUBLIC_TOOL_NAMES = ("terminal_exec", "terminal_start", "terminal_poll", "terminal_control", "capacity_observer", "fs_read_batch")
SERVER_DESCRIPTION = "Bounded local command execution and advisory capacity MCP server; terminal tools may modify the host."
SERVER_INSTRUCTIONS = (
    "Execute literal argv with shell=False and no implicit shell. "
    "Use an absolute cwd under the configured workspace root. "
    "terminal_exec is one-shot; terminal_start begins a PTY lifecycle managed with "
    "terminal_poll and terminal_control. Terminal execution uses the operator account's normal permissions "
    "and may modify the host. Protected Runtime filtering is defense-in-depth for recognized argv, shell, "
    "and wrapper lifecycle intent; it is not a sandbox, filesystem confinement, privilege isolation, "
    "syscall filter, or complete prevention of arbitrary same-UID effects. Tools expose bounded output. "
    "capacity_observer provides read-only advisory capacity information. "
    "fs_read_batch performs read-only ordered cwd-relative UTF-8 file reads for at most 20 items "
    "with fixed output and scan-work ceilings and per-item filesystem failures."
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
    rows: TerminalDimension | None = None,
    cols: TerminalDimension | None = None,
) -> TerminalControlResult:
    """Write, interrupt, terminate, or resize one persistent PTY session whose process may modify the host."""

    return cast(
        TerminalControlResult,
        _call_runtime_tool(_control_terminal, session_id, action, data, rows, cols),
    )


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def capacity_observer() -> CapacityObserverResult:
    """Report a bounded read-only advisory machine-capacity ceiling."""

    return cast(CapacityObserverResult, _call_runtime_tool(observe_capacity))


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def fs_read_batch(cwd: AbsoluteCwd, items: FsReadItems) -> FsReadBatchResult:
    """Read bounded ordered UTF-8 file ranges below one validated cwd."""

    return cast(FsReadBatchResult, _call_runtime_tool(read_files_batch, cwd, items))


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
