from __future__ import annotations

import asyncio
import io
import importlib
import inspect
import json
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import patch

from agent_runtime.timing import (
    timing_middleware,
    timed_tool_call,
)


ALLOWED_EVENT_KEYS = {
    "event_kind",
    "runtime_call_id",
    "raw_request_id",
    "request_id_type",
    "tool_name",
    "wall_clock_start",
    "wall_clock_end",
    "monotonic_duration_ms",
    "outcome",
    "process_kind",
    "termination_state",
}


class TimingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_request_ids_are_correlated_by_distinct_runtime_ids(self) -> None:
        output = io.StringIO()

        async def call_next(ctx):
            await asyncio.sleep(ctx.params["delay"])
            return await asyncio.to_thread(
                timed_tool_call,
                "terminal_exec",
                lambda: {"operator_secret": "must-not-be-logged"},
            )

        first = SimpleNamespace(
            method="tools/call",
            request_id=17,
            params={"name": "terminal_exec", "delay": 0.03},
        )
        second = SimpleNamespace(
            method="tools/call",
            request_id=17,
            params={"name": "terminal_exec", "delay": 0.005},
        )

        with patch("agent_runtime.timing.sys.stderr", output):
            results = await asyncio.gather(
                timing_middleware(first, call_next),
                timing_middleware(second, call_next),
            )

        self.assertEqual(len(results), 2)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 6)
        self.assertTrue(all(set(event) <= ALLOWED_EVENT_KEYS for event in events))
        self.assertNotIn("operator_secret", output.getvalue())

        request_starts = [event for event in events if event["event_kind"] == "mcp_request_start"]
        request_ends = [event for event in events if event["event_kind"] == "mcp_request_end"]
        tool_ends = [event for event in events if event["event_kind"] == "tool_execution_end"]
        self.assertEqual(len(request_starts), 2)
        self.assertEqual(len(request_ends), 2)
        self.assertEqual(len(tool_ends), 2)
        self.assertEqual({event["raw_request_id"] for event in request_starts}, {17})
        self.assertEqual({event["request_id_type"] for event in request_starts}, {"int"})
        runtime_ids = {event["runtime_call_id"] for event in events}
        self.assertEqual(len(runtime_ids), 2)
        for runtime_call_id in runtime_ids:
            correlated = [event for event in events if event["runtime_call_id"] == runtime_call_id]
            self.assertEqual(
                {event["event_kind"] for event in correlated},
                {"mcp_request_start", "tool_execution_end", "mcp_request_end"},
            )
            self.assertEqual({event["tool_name"] for event in correlated}, {"terminal_exec"})

        self.assertTrue(
            all(isinstance(event["monotonic_duration_ms"], float) for event in request_ends)
        )
        self.assertGreater(
            max(event["monotonic_duration_ms"] for event in request_ends),
            min(event["monotonic_duration_ms"] for event in request_ends),
        )

    async def test_error_and_cancellation_completion_events_are_bounded(self) -> None:
        output = io.StringIO()

        async def raises(_ctx):
            raise RuntimeError("exception sentinel must-not-be-logged")

        async def cancels(_ctx):
            raise asyncio.CancelledError("cancel sentinel must-not-be-logged")

        error_ctx = SimpleNamespace(
            method="tools/call",
            request_id="same-id",
            params={"name": "terminal_exec", "arguments": {"secret": "hidden"}},
        )
        cancel_ctx = SimpleNamespace(
            method="tools/call",
            request_id="same-id",
            params={"name": "terminal_exec", "arguments": {"secret": "hidden"}},
        )

        with patch("agent_runtime.timing.sys.stderr", output):
            with self.assertRaisesRegex(RuntimeError, "exception sentinel"):
                await timing_middleware(error_ctx, raises)
            with self.assertRaises(asyncio.CancelledError):
                await timing_middleware(cancel_ctx, cancels)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        ends = [event for event in events if event["event_kind"] == "mcp_request_end"]
        self.assertEqual(len(ends), 2)
        self.assertEqual({event["request_id_type"] for event in ends}, {"str"})
        self.assertEqual({event["outcome"] for event in ends}, {"error", "cancelled"})
        self.assertNotIn("exception sentinel", output.getvalue())
        self.assertNotIn("cancel sentinel", output.getvalue())
        self.assertNotIn("hidden", output.getvalue())

    async def test_non_tools_calls_pass_through_without_diagnostics(self) -> None:
        output = io.StringIO()
        context = SimpleNamespace(method="ping", request_id=1, params=None)

        async def call_next(_ctx):
            return {"ok": True}

        with patch("agent_runtime.timing.sys.stderr", output):
            result = await timing_middleware(context, call_next)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(output.getvalue(), "")


class TimingToolTests(unittest.TestCase):
    def test_real_mcp_registration_keeps_surface_and_installs_middleware(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            server = importlib.import_module("agent_runtime.server")

        self.assertEqual(tuple(tool.name for tool in server.mcp._tool_manager.list_tools()), (
            "terminal_exec",
            "terminal_start",
            "terminal_poll",
            "terminal_control",
        ))
        self.assertIn(timing_middleware, server.mcp.middleware)
        self.assertEqual(
            list(inspect.signature(server.terminal_exec).parameters),
            ["argv", "cwd", "timeout_seconds"],
        )
        self.assertEqual(
            [parameter.name for parameter in inspect.signature(server.terminal_control).parameters.values()],
            ["session_id", "action", "data", "rows", "cols"],
        )

    def test_tool_execution_event_has_no_arguments_or_result_payload(self) -> None:
        output = io.StringIO()
        from agent_runtime.timing import bind_call_context, reset_call_context

        context, token = bind_call_context("request-secret")
        try:
            with patch("agent_runtime.timing.sys.stderr", output):
                result = timed_tool_call(
                    "terminal_control",
                    lambda *args, **kwargs: {"secret-result": "no-log"},
                    "session-secret",
                    action="write-secret",
                    data="operator-secret",
                )
        finally:
            reset_call_context(token)

        self.assertEqual(result, {"secret-result": "no-log"})
        event = json.loads(output.getvalue())
        self.assertEqual(event["event_kind"], "tool_execution_end")
        self.assertEqual(event["runtime_call_id"], context.runtime_call_id)
        self.assertEqual(event["tool_name"], "terminal_control")
        self.assertEqual(event["outcome"], "ok")
        self.assertNotIn("session-secret", output.getvalue())
        self.assertNotIn("operator-secret", output.getvalue())
        self.assertNotIn("no-log", output.getvalue())


if __name__ == "__main__":
    unittest.main()
