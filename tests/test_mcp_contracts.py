from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
EXPECTED_TOOLS = (
    "terminal_exec",
    "terminal_start",
    "terminal_poll",
    "terminal_control",
    "capacity_observer",
)
EXPECTED_ANNOTATIONS = {
    "terminal_exec": (False, True, False, True),
    "terminal_start": (False, True, False, True),
    "terminal_poll": (False, False, False, False),
    "terminal_control": (False, True, False, True),
    "capacity_observer": (True, False, True, False),
}


def _annotation_tuple(tool: object) -> tuple[bool, bool, bool, bool]:
    annotations = getattr(tool, "annotations")
    values = annotations.model_dump(by_alias=True)
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


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
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

    async def _tools(self) -> dict[str, object]:
        return {tool.name: tool for tool in await server.mcp.list_tools()}

    async def _assert_sdk_rejects(self, name: str, arguments: dict[str, object]) -> None:
        try:
            result = await server.mcp.call_tool(name, arguments)
        except Exception:
            return
        self.assertTrue(getattr(result, "is_error", False), result)

    def _assert_closed_typed_output(self, schema: dict[str, object]) -> None:
        objects = [
            node
            for node in _walk_schema(schema)
            if node.get("type") == "object" and "properties" in node
        ]
        self.assertTrue(objects, schema)
        for node in objects:
            self.assertIs(node.get("additionalProperties"), False, node)

    async def test_exact_five_tool_surface_and_annotations_are_preserved(self) -> None:
        tools = await server.mcp.list_tools()
        self.assertEqual(tuple(tool.name for tool in tools), EXPECTED_TOOLS)
        for tool in tools:
            self.assertEqual(_annotation_tuple(tool), EXPECTED_ANNOTATIONS[tool.name])

    async def test_input_schemas_publish_existing_representable_bounds(self) -> None:
        tools = await self._tools()

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

    async def test_all_output_schemas_are_closed_and_enumerate_supported_fields(self) -> None:
        tools = await self._tools()
        expected_fields = {
            "terminal_exec": {
                "cwd",
                "argv",
                "exit_code",
                "timed_out",
                "stdout",
                "stderr",
                "stdout_truncated",
                "stderr_truncated",
            },
            "terminal_start": {
                "session_id",
                "status",
                "output",
                "next_cursor",
                "cursor_expired",
                "dropped_output_bytes",
                "exit_code",
            },
            "terminal_poll": {
                "session_id",
                "status",
                "output",
                "next_cursor",
                "cursor_expired",
                "dropped_output_bytes",
                "exit_code",
            },
            "terminal_control": {"session_id", "status", "exit_code"},
            "capacity_observer": {
                "schema_version",
                "capacity_parallelism_ceiling",
                "reason_codes",
                "signals",
                "active_processors",
                "load1",
                "cpu_busy_pct",
                "sampled_window_ms",
                "thermal_state",
                "swap_used_bytes",
                "swap_total_bytes",
                "swapin_delta_pages",
                "swapout_delta_pages",
                "vm_free_bytes",
                "vm_inactive_bytes",
                "vm_purgeable_bytes",
                "vm_compressor_bytes",
                "disk_available_bytes",
                "probe_status",
            },
        }
        for name, expected in expected_fields.items():
            schema = tools[name].output_schema
            self.assertIsNotNone(schema, name)
            self._assert_closed_typed_output(schema)
            field_names = {
                field
                for node in _walk_schema(schema)
                for field in node.get("properties", {})
            }
            self.assertEqual(field_names, expected, name)

    def test_sdk_pin_is_exactly_mcp_2_2_0(self) -> None:
        self.assertEqual((ROOT / "requirements.txt").read_text(), "mcp==2.2.0\n")

    async def test_server_metadata_is_explicit_and_machine_useful(self) -> None:
        self.assertEqual(server.mcp.name, "Agent Runtime")
        self.assertEqual(server.mcp.version, "0.2.0")
        self.assertTrue(server.mcp.description.strip())
        instructions = server.mcp.instructions
        self.assertTrue(instructions.strip())
        for phrase in (
            "literal argv",
            "shell=False",
            "absolute cwd",
            "workspace root",
            "terminal_exec",
            "terminal_start",
            "terminal_poll",
            "terminal_control",
            "bounded output",
            "capacity_observer",
            "read-only advisory",
        ):
            self.assertIn(phrase, instructions)

    async def test_representative_successful_results_satisfy_declared_output_schemas(self) -> None:
        tools = await self._tools()

        exec_result = await server.mcp.call_tool(
            "terminal_exec",
            {
                "argv": ["/usr/bin/printf", "contract-ok"],
                "cwd": str(ROOT),
                "timeout_seconds": 5,
            },
        )
        self.assertFalse(exec_result.is_error)
        Draft202012Validator(tools["terminal_exec"].output_schema).validate(
            exec_result.structured_content
        )

        session_id: str | None = None
        terminated = False
        try:
            start_result = await server.mcp.call_tool(
                "terminal_start",
                {"argv": ["/bin/cat"], "cwd": str(ROOT)},
            )
            self.assertFalse(start_result.is_error)
            Draft202012Validator(tools["terminal_start"].output_schema).validate(
                start_result.structured_content
            )
            self.assertEqual(start_result.structured_content["status"], "running")
            self.assertNotIn("exit_code", start_result.structured_content)
            session_id = start_result.structured_content["session_id"]

            poll_result = await server.mcp.call_tool(
                "terminal_poll",
                {"session_id": session_id, "cursor": 0, "wait_ms": 0},
            )
            self.assertFalse(poll_result.is_error)
            Draft202012Validator(tools["terminal_poll"].output_schema).validate(
                poll_result.structured_content
            )
            self.assertEqual(poll_result.structured_content["status"], "running")
            self.assertNotIn("exit_code", poll_result.structured_content)

            control_result = await server.mcp.call_tool(
                "terminal_control",
                {"session_id": session_id, "action": "terminate"},
            )
            self.assertFalse(control_result.is_error)
            Draft202012Validator(tools["terminal_control"].output_schema).validate(
                control_result.structured_content
            )
            self.assertEqual(control_result.structured_content["status"], "exited")
            self.assertIn("exit_code", control_result.structured_content)
            terminated = True
        finally:
            if session_id is not None and not terminated:
                try:
                    await server.mcp.call_tool(
                        "terminal_control",
                        {"session_id": session_id, "action": "terminate"},
                    )
                except Exception:
                    pass

        capacity_result = await server.mcp.call_tool("capacity_observer", {})
        self.assertFalse(capacity_result.is_error)
        Draft202012Validator(tools["capacity_observer"].output_schema).validate(
            capacity_result.structured_content
        )

    async def test_invalid_boundary_values_are_rejected_before_tool_bodies_run(self) -> None:
        cases = (
            ("terminal_exec", "execute_terminal", {"argv": ["/usr/bin/true"], "cwd": str(ROOT), "timeout_seconds": 0}),
            ("terminal_exec", "execute_terminal", {"argv": ["/usr/bin/true"], "cwd": str(ROOT), "timeout_seconds": 3600.1}),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": -1, "wait_ms": 0}),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": 0, "wait_ms": -1}),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": 0, "wait_ms": 1001}),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "invalid"}),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "resize", "rows": 0, "cols": 24}),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "resize", "rows": 65536, "cols": 24}),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "resize", "rows": 24, "cols": 0}),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "resize", "rows": 24, "cols": 65536}),
        )
        for tool_name, delegate_name, arguments in cases:
            with self.subTest(tool=tool_name, arguments=arguments):
                with patch.object(server, delegate_name) as delegate:
                    await self._assert_sdk_rejects(tool_name, arguments)
                    delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
