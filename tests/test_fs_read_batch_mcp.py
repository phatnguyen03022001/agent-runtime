from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from mcp import Client
from mcp.types import TextContent

from agent_runtime import server
from agent_runtime.timing import ALLOWED_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
ERROR_CODES = {
    "NOT_FOUND",
    "ACCESS_DENIED",
    "SYMLINK_DISALLOWED",
    "NOT_REGULAR_FILE",
    "INVALID_UTF8",
    "ITEM_OUTPUT_LIMIT_EXCEEDED",
    "BATCH_OUTPUT_LIMIT_EXCEEDED",
    "ITEM_SCAN_LIMIT_EXCEEDED",
    "BATCH_SCAN_LIMIT_EXCEEDED",
    "READ_FAILED",
}


def _integer_branch(schema: dict[str, object]) -> dict[str, object]:
    if schema.get("type") == "integer":
        return schema
    for candidate in schema.get("anyOf", []):
        if isinstance(candidate, dict) and candidate.get("type") == "integer":
            return candidate
    raise AssertionError(f"no integer branch in {schema!r}")


def _result_text(result: object) -> str:
    blocks = getattr(result, "content")
    return "\n".join(block.text for block in blocks if isinstance(block, TextContent))


class FsReadBatchMCPTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_public_schema_is_bounded_closed_and_has_no_limit_knobs(self) -> None:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        tool = tools["fs_read_batch"]
        props = tool.input_schema["properties"]
        self.assertEqual(set(props), {"cwd", "items"})
        self.assertNotIn("pattern", props["cwd"])
        self.assertEqual(props["items"]["minItems"], 1)
        self.assertEqual(props["items"]["maxItems"], 20)

        item = tool.input_schema["$defs"]["FsReadItem"]
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(item["properties"]["path"]["minLength"], 1)
        self.assertEqual(item["properties"]["path"]["maxLength"], 4096)
        self.assertEqual(_integer_branch(item["properties"]["start_line"])["minimum"], 1)
        self.assertEqual(_integer_branch(item["properties"]["end_line"])["minimum"], 1)

        output = tool.output_schema
        self.assertIs(output["additionalProperties"], False)
        for definition in output["$defs"].values():
            self.assertIs(definition["additionalProperties"], False)
        error = output["$defs"]["FsReadErrorResult"]
        self.assertEqual(set(error["properties"]["error_code"]["enum"]), ERROR_CODES)
        self.assertEqual(error["properties"]["message"]["maxLength"], 160)

    async def test_public_client_structured_success_and_partial_error(self) -> None:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "fs_read_batch",
                {
                    "cwd": str(ROOT),
                    "items": [
                        {"path": "README.md", "start_line": 1, "end_line": 1},
                        {"path": "definitely-missing-task0040.txt"},
                    ],
                },
            )
        self.assertFalse(result.is_error)
        Draft202012Validator(tools["fs_read_batch"].output_schema).validate(result.structured_content)
        first, second = result.structured_content["items"]
        self.assertEqual(first["status"], "ok")
        self.assertTrue(first["text"].startswith("# agent-runtime"))
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["error_code"], "NOT_FOUND")
        self.assertNotIn("text", second)
        self.assertNotIn(str(WORKSPACE_ROOT), result.model_dump_json(by_alias=True))

    async def test_lexical_invalid_request_uses_deliberate_tool_error(self) -> None:
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "fs_read_batch",
                {"cwd": str(ROOT), "items": [{"path": "sub/../README.md"}]},
            )
        self.assertTrue(result.is_error)
        text = _result_text(result)
        self.assertIn("dot-dot", text)
        self.assertNotIn("Traceback", text)
        self.assertLessEqual(len(text), 256)

    async def test_unexpected_exception_is_sdk_sanitized(self) -> None:
        sentinel = "TASK0040_UNEXPECTED_SECRET_SENTINEL"
        with patch.object(server, "read_files_batch", side_effect=RuntimeError(sentinel)):
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "fs_read_batch",
                    {"cwd": str(ROOT), "items": [{"path": "README.md"}]},
                )
        self.assertTrue(result.is_error)
        text = _result_text(result)
        self.assertEqual(text, "Error executing tool fs_read_batch")
        self.assertNotIn(sentinel, result.model_dump_json(by_alias=True))

    async def test_schema_rejects_cardinality_and_line_bounds_before_delegate(self) -> None:
        cases = [
            {"cwd": str(ROOT), "items": []},
            {"cwd": str(ROOT), "items": [{"path": "README.md"}] * 21},
            {"cwd": str(ROOT), "items": [{"path": "README.md", "start_line": 0}]},
            {"cwd": str(ROOT), "items": [{"path": "README.md", "end_line": 0}]},
            {"cwd": str(ROOT), "items": [{"path": "x" * 4097}]},
        ]
        with patch.object(server, "read_files_batch") as delegate:
            async with Client(server.mcp) as client:
                for arguments in cases:
                    with self.subTest(arguments=arguments):
                        result = await client.call_tool("fs_read_batch", arguments)
                        self.assertTrue(result.is_error)
        delegate.assert_not_called()

    def test_timing_allowlist_and_server_instructions_cover_public_surface(self) -> None:
        self.assertEqual(
            ALLOWED_TOOL_NAMES,
            frozenset({
                "terminal_exec", "terminal_start", "terminal_poll",
                "terminal_control", "terminal_resize", "capacity_observer", "fs_read_batch",
                "repo_observer",
            }),
        )
        self.assertIn("fs_read_batch", server.SERVER_INSTRUCTIONS)
        self.assertIn("cwd-relative", server.SERVER_INSTRUCTIONS)
        self.assertIn("read-only", server.SERVER_INSTRUCTIONS)
