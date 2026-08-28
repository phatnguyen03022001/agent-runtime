from __future__ import annotations

import inspect
from typing import Any, Callable

try:
    from mcp.server import MCPServer
except ImportError:
    from mcp.server.mcpserver import MCPServer

try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except ImportError:
    _ToolAnnotations = None

from .executor import execute_terminal

PUBLIC_TOOL_NAMES = ("terminal_exec",)
mcp = MCPServer("Agent Runtime")


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
        if annotations is not None:
            try:
                return mcp.tool(annotations=annotations)(function)
            except (TypeError, ValueError):
                pass
        return mcp.tool()(function)

    return decorator


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
def terminal_exec(
    argv: list[str],
    cwd: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Run one literal local argv; this capability may modify the host."""

    return execute_terminal(argv, cwd, timeout_seconds)


def _main() -> int:
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
