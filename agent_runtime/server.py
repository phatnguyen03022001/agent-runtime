from __future__ import annotations

import inspect
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

try:
    from mcp.server import MCPServer
except ImportError:
    from mcp.server.mcpserver import MCPServer

try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except ImportError:
    _ToolAnnotations = None

from .runner import AgentRuntime

CONFIG_ENV = "AGENT_RUNTIME_CONFIG"
PUBLIC_TOOL_NAMES = ("get_head", "sync", "run_verify", "get_last_log")
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
    *, read_only: bool, destructive: bool, idempotent: bool, open_world: bool
) -> Any | None:
    if _ToolAnnotations is None or not _tool_annotations_supported():
        return None
    field_names: set[str] = set()
    for attribute in ("model_fields", "__fields__", "__annotations__"):
        fields = getattr(_ToolAnnotations, attribute, None)
        if isinstance(fields, dict):
            field_names.update(str(name) for name in fields)
    if not field_names:
        try:
            signature = inspect.signature(_ToolAnnotations)
        except (TypeError, ValueError):
            return None
        field_names.update(signature.parameters)
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
        return None
    try:
        return _ToolAnnotations(**kwargs)
    except (TypeError, ValueError):
        return None


def _tool(
    *, read_only: bool, destructive: bool, idempotent: bool, open_world: bool
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


@lru_cache(maxsize=1)
def _runtime() -> AgentRuntime:
    raw = os.environ.get(CONFIG_ENV)
    if not raw:
        raise RuntimeError(f"{CONFIG_ENV} must point to the trusted local TOML config.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{CONFIG_ENV} must be an absolute path.")
    return AgentRuntime(path)


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def get_head(project: str) -> dict[str, Any]:
    """Read bounded local Git metadata for one exact trusted project ID."""
    return _runtime().get_head(project)


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
def sync(project: str) -> dict[str, Any]:
    """Destructively synchronize one trusted disposable checkout to its profile branch."""
    return _runtime().sync(project)


@_tool(read_only=False, destructive=False, idempotent=False, open_world=True)
def run_verify(project: str) -> dict[str, Any]:
    """Launch detached verification using only the trusted profile verifier argv."""
    return _runtime().run_verify(project)


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def get_last_log(project: str) -> dict[str, Any]:
    """Read the latest bounded verification status and diagnostic log tail."""
    return _runtime().get_last_log(project)


def _main() -> int:
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
