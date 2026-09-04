from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar


ALLOWED_TOOL_NAMES = frozenset(
    {"terminal_exec", "terminal_start", "terminal_poll", "terminal_control"}
)

_CURRENT_CALL: contextvars.ContextVar[TimingContext | None] = contextvars.ContextVar(
    "agent_runtime_timing_call",
    default=None,
)
_OUTPUT_LOCK = threading.Lock()
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class TimingContext:
    runtime_call_id: str
    raw_request_id: str | int | None
    request_id_type: str


def bind_call_context(raw_request_id: str | int | None) -> tuple[TimingContext, contextvars.Token]:
    context = TimingContext(
        runtime_call_id=secrets.token_hex(16),
        raw_request_id=raw_request_id,
        request_id_type="none" if raw_request_id is None else type(raw_request_id).__name__,
    )
    return context, _CURRENT_CALL.set(context)


def reset_call_context(token: contextvars.Token) -> None:
    _CURRENT_CALL.reset(token)


def current_call_context() -> TimingContext | None:
    return _CURRENT_CALL.get()


def _allowlisted_tool_name(tool_name: object) -> str | None:
    if isinstance(tool_name, str) and tool_name in ALLOWED_TOOL_NAMES:
        return tool_name
    return None


def _request_tool_name(ctx: object) -> str | None:
    params = getattr(ctx, "params", None)
    if not isinstance(params, Mapping):
        return None
    return _allowlisted_tool_name(params.get("name"))


def _exception_outcome(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError) or type(exc).__name__ == "CancelledError":
        return "cancelled"
    return "error"


def _result_outcome(result: object) -> str:
    if isinstance(result, Mapping) and result.get("isError") is True:
        return "error"
    if getattr(result, "is_error", False) is True:
        return "error"
    return "ok"


def _emit(event: Mapping[str, Any]) -> None:
    try:
        line = json.dumps(dict(event), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with _OUTPUT_LOCK:
            sys.stderr.write(line)
            sys.stderr.write("\n")
            sys.stderr.flush()
    except Exception:
        # Diagnostics must never change a tool result or cleanup path.
        return


def _common_event(event_kind: str, context: TimingContext) -> dict[str, Any]:
    return {
        "event_kind": event_kind,
        "runtime_call_id": context.runtime_call_id,
        "raw_request_id": context.raw_request_id,
        "request_id_type": context.request_id_type,
    }


def _duration_ms(started: float, ended: float) -> float:
    return max(0.0, (ended - started) * 1000.0)


def timed_tool_call(
    tool_name: str,
    function: Callable[..., _ResultT],
    *args: Any,
    **kwargs: Any,
) -> _ResultT:
    context = current_call_context()
    if context is None:
        return function(*args, **kwargs)

    started_wall = time.time()
    started_mono = time.monotonic()
    outcome = "ok"
    try:
        return function(*args, **kwargs)
    except BaseException as exc:
        outcome = _exception_outcome(exc)
        raise
    finally:
        ended_mono = time.monotonic()
        event = _common_event("tool_execution_end", context)
        event.update(
            {
                "tool_name": _allowlisted_tool_name(tool_name),
                "wall_clock_start": started_wall,
                "wall_clock_end": time.time(),
                "monotonic_duration_ms": _duration_ms(started_mono, ended_mono),
                "outcome": outcome,
            }
        )
        _emit(event)


def emit_process_end(
    context: TimingContext | None,
    *,
    tool_name: str,
    process_kind: str,
    started_wall: float,
    started_mono: float,
    termination_state: str,
) -> None:
    if context is None:
        return
    ended_mono = time.monotonic()
    event = _common_event("process_end", context)
    event.update(
        {
            "tool_name": _allowlisted_tool_name(tool_name),
            "wall_clock_start": started_wall,
            "wall_clock_end": time.time(),
            "monotonic_duration_ms": _duration_ms(started_mono, ended_mono),
            "outcome": (
                "ok"
                if termination_state in {"completed", "explicit_terminate", "idle_reap", "shutdown"}
                else "timed_out"
                if termination_state == "timed_out"
                else "error"
            ),
            "process_kind": process_kind,
            "termination_state": termination_state,
        }
    )
    _emit(event)


async def timing_middleware(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
    if getattr(ctx, "method", None) != "tools/call":
        return await call_next(ctx)

    context, token = bind_call_context(getattr(ctx, "request_id", None))
    tool_name = _request_tool_name(ctx)
    started_wall = time.time()
    started_mono = time.monotonic()
    start_event = _common_event("mcp_request_start", context)
    start_event["wall_clock_start"] = started_wall
    if tool_name is not None:
        start_event["tool_name"] = tool_name
    _emit(start_event)

    outcome = "ok"
    try:
        result = await call_next(ctx)
        outcome = _result_outcome(result)
        return result
    except BaseException as exc:
        outcome = _exception_outcome(exc)
        raise
    finally:
        ended_mono = time.monotonic()
        end_event = _common_event("mcp_request_end", context)
        end_event.update(
            {
                "wall_clock_start": started_wall,
                "wall_clock_end": time.time(),
                "monotonic_duration_ms": _duration_ms(started_mono, ended_mono),
                "outcome": outcome,
            }
        )
        if tool_name is not None:
            end_event["tool_name"] = tool_name
        _emit(end_event)
        reset_call_context(token)


def timed_tool_wrapper(
    tool_name: str,
    function: Callable[..., _ResultT],
) -> Callable[..., _ResultT]:
    """Return a signature-preserving synchronous wrapper for a tool body."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> _ResultT:
        return timed_tool_call(tool_name, function, *args, **kwargs)

    return wrapped
