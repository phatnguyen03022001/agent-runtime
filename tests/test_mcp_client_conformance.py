from __future__ import annotations

import importlib.metadata
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from mcp import Client
from mcp.types import TextContent

from agent_runtime import server

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
EXPECTED_TOOLS = (
    "terminal_exec",
    "terminal_start",
    "terminal_poll",
    "terminal_control",
    "capacity_observer",
    "fs_read_batch",
)
EXPECTED_ANNOTATIONS = {
    "terminal_exec": (False, True, False, True),
    "terminal_start": (False, True, False, True),
    "terminal_poll": (False, False, False, False),
    "terminal_control": (False, True, False, True),
    "capacity_observer": (True, False, True, False),
    "fs_read_batch": (True, False, True, False),
}
EXPECTED_OUTPUT_FIELDS = {
    "terminal_exec": {
        "cwd", "argv", "exit_code", "timed_out", "stdout", "stderr",
        "stdout_truncated", "stderr_truncated",
    },
    "terminal_start": {
        "session_id", "status", "output", "next_cursor", "cursor_expired",
        "dropped_output_bytes", "exit_code",
    },
    "terminal_poll": {
        "session_id", "status", "output", "next_cursor", "cursor_expired",
        "dropped_output_bytes", "exit_code",
    },
    "terminal_control": {"session_id", "status", "exit_code"},
    "capacity_observer": {
        "schema_version", "capacity_parallelism_ceiling", "reason_codes", "signals",
        "active_processors", "load1", "cpu_busy_pct", "sampled_window_ms",
        "thermal_state", "swap_used_bytes", "swap_total_bytes", "swapin_delta_pages",
        "swapout_delta_pages", "vm_free_bytes", "vm_inactive_bytes",
        "vm_purgeable_bytes", "vm_compressor_bytes", "disk_available_bytes",
        "probe_status",
    },
    "fs_read_batch": {
        "items", "status", "path", "start_line", "end_line",
        "text", "error_code", "message",
    },
}


def _annotation_tuple(tool: object) -> tuple[bool, bool, bool, bool]:
    values = tool.annotations.model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


def _walk_schema(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


def _integer_branch(schema: dict[str, object]) -> dict[str, object]:
    if schema.get("type") == "integer":
        return schema
    for candidate in schema.get("anyOf", []):
        if isinstance(candidate, dict) and candidate.get("type") == "integer":
            return candidate
    raise AssertionError(f"no integer branch in {schema!r}")


def _result_text(result: object) -> str:
    blocks = getattr(result, "content")
    texts = [block.text for block in blocks if isinstance(block, TextContent)]
    return "\n".join(texts)


class MCPClientConformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(WORKSPACE_ROOT),
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_development_sdk_and_requirement_are_exactly_2_2_0(self) -> None:
        self.assertEqual(importlib.metadata.version("mcp"), "2.2.0")
        self.assertEqual((ROOT / "requirements.txt").read_text(), "mcp==2.2.0\n")

    async def test_public_client_negotiates_metadata_and_exact_tool_contract(self) -> None:
        async with Client(server.mcp) as client:
            self.assertEqual(client.protocol_version, "2026-07-28")
            self.assertIsNotNone(client.server_info)
            self.assertEqual(client.server_info.name, "Agent Runtime")
            self.assertEqual(client.server_info.version, "0.2.0")
            self.assertEqual(client.instructions, server.SERVER_INSTRUCTIONS)
            for phrase in (
                "may modify the host",
                "defense-in-depth",
                "not a sandbox",
                "filesystem confinement",
                "privilege isolation",
                "same-UID",
            ):
                self.assertIn(phrase, client.instructions)

            listing = await client.list_tools()
            self.assertEqual(tuple(tool.name for tool in listing.tools), EXPECTED_TOOLS)
            tools = {tool.name: tool for tool in listing.tools}
            for name in ("terminal_exec", "terminal_start", "terminal_control"):
                self.assertIn("may modify the host", tools[name].description)
            for name, annotations in EXPECTED_ANNOTATIONS.items():
                self.assertEqual(_annotation_tuple(tools[name]), annotations)

            exec_props = tools["terminal_exec"].input_schema["properties"]
            self.assertEqual(exec_props["argv"]["minItems"], 1)
            self.assertEqual(exec_props["argv"]["items"]["type"], "string")
            self.assertEqual(exec_props["cwd"]["minLength"], 1)
            self.assertEqual(exec_props["cwd"]["pattern"], "^/")
            self.assertEqual(exec_props["timeout_seconds"]["exclusiveMinimum"], 0)
            self.assertEqual(exec_props["timeout_seconds"]["maximum"], 3600)
            self.assertEqual(exec_props["timeout_seconds"]["default"], 300)

            start_props = tools["terminal_start"].input_schema["properties"]
            self.assertEqual(start_props["argv"]["minItems"], 1)
            self.assertEqual(start_props["cwd"]["minLength"], 1)
            self.assertEqual(start_props["cwd"]["pattern"], "^/")

            poll_props = tools["terminal_poll"].input_schema["properties"]
            self.assertEqual(poll_props["session_id"]["minLength"], 1)
            self.assertEqual(poll_props["cursor"]["minimum"], 0)
            self.assertEqual(poll_props["wait_ms"]["minimum"], 0)
            self.assertEqual(poll_props["wait_ms"]["maximum"], 1000)

            control_props = tools["terminal_control"].input_schema["properties"]
            self.assertEqual(control_props["session_id"]["minLength"], 1)
            self.assertEqual(
                control_props["action"]["enum"],
                ["write", "interrupt", "terminate", "resize"],
            )
            for field in ("rows", "cols"):
                integer = _integer_branch(control_props[field])
                self.assertEqual(integer["minimum"], 1)
                self.assertEqual(integer["maximum"], 65535)

            for name, tool in tools.items():
                self.assertIsNotNone(tool.output_schema, name)
                objects = [
                    node
                    for node in _walk_schema(tool.output_schema)
                    if node.get("type") == "object" and "properties" in node
                ]
                self.assertTrue(objects, name)
                for node in objects:
                    self.assertIs(node.get("additionalProperties"), False, (name, node))
                field_names = {
                    field
                    for node in _walk_schema(tool.output_schema)
                    for field in node.get("properties", {})
                }
                self.assertEqual(field_names, EXPECTED_OUTPUT_FIELDS[name], name)

            self.assertEqual((await client.list_resources()).resources, [])
            self.assertEqual((await client.list_prompts()).prompts, [])

    async def test_public_client_structured_successes_validate_including_pty_lifecycle(self) -> None:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}

            exec_result = await client.call_tool(
                "terminal_exec",
                {
                    "argv": ["/usr/bin/printf", "client-contract-ok"],
                    "cwd": str(ROOT),
                    "timeout_seconds": 5,
                },
            )
            self.assertFalse(exec_result.is_error)
            Draft202012Validator(tools["terminal_exec"].output_schema).validate(
                exec_result.structured_content
            )

            capacity_result = await client.call_tool("capacity_observer", {})
            self.assertFalse(capacity_result.is_error)
            Draft202012Validator(tools["capacity_observer"].output_schema).validate(
                capacity_result.structured_content
            )

            session_id: str | None = None
            terminated = False
            try:
                start_result = await client.call_tool(
                    "terminal_start",
                    {"argv": ["/bin/cat"], "cwd": str(ROOT)},
                )
                self.assertFalse(start_result.is_error)
                Draft202012Validator(tools["terminal_start"].output_schema).validate(
                    start_result.structured_content
                )
                session_id = start_result.structured_content["session_id"]

                poll_result = await client.call_tool(
                    "terminal_poll",
                    {"session_id": session_id, "cursor": 0, "wait_ms": 0},
                )
                self.assertFalse(poll_result.is_error)
                Draft202012Validator(tools["terminal_poll"].output_schema).validate(
                    poll_result.structured_content
                )

                control_result = await client.call_tool(
                    "terminal_control",
                    {"session_id": session_id, "action": "terminate"},
                )
                self.assertFalse(control_result.is_error)
                Draft202012Validator(tools["terminal_control"].output_schema).validate(
                    control_result.structured_content
                )
                terminated = True
            finally:
                if session_id is not None and not terminated:
                    try:
                        await client.call_tool(
                            "terminal_control",
                            {"session_id": session_id, "action": "terminate"},
                        )
                    except Exception:
                        pass

    async def test_expected_domain_failure_uses_deliberate_tool_error_path(self) -> None:
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "terminal_poll",
                {"session_id": "missing-session", "cursor": 0, "wait_ms": 0},
            )
        self.assertTrue(result.is_error)
        text = _result_text(result)
        self.assertIn("unknown or expired session_id", text)
        self.assertNotIn("Traceback", text)
        self.assertLessEqual(len(text), 256)

    async def test_unexpected_exception_is_sanitized_and_does_not_leak_sentinel(self) -> None:
        sentinel = "TASK0039_UNEXPECTED_SECRET_SENTINEL"
        with patch.object(server, "execute_terminal", side_effect=RuntimeError(sentinel)):
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "terminal_exec",
                    {
                        "argv": ["/usr/bin/true"],
                        "cwd": str(ROOT),
                        "timeout_seconds": 5,
                    },
                )
        self.assertTrue(result.is_error)
        text = _result_text(result)
        self.assertEqual(text, "Error executing tool terminal_exec")
        self.assertNotIn(sentinel, text)
        self.assertNotIn("RuntimeError", text)
        self.assertNotIn("Traceback", text)
        serialized = result.model_dump_json(by_alias=True)
        self.assertNotIn(sentinel, serialized)
        self.assertNotIn("Traceback", serialized)

    async def test_invalid_schema_is_rejected_before_delegate_executes(self) -> None:
        with patch.object(server, "execute_terminal") as delegate:
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "terminal_exec",
                    {
                        "argv": ["/usr/bin/true"],
                        "cwd": str(ROOT),
                        "timeout_seconds": 0,
                    },
                )
        self.assertTrue(result.is_error)
        delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
