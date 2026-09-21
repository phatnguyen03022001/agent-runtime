from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server
from agent_runtime.timing import ALLOWED_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent


def _annotation_tuple(tool: object) -> tuple[bool, bool, bool, bool]:
    values = getattr(tool, "annotations").model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


class RepoObserverMCPTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_repo_observer_is_public_tool_12_with_closed_read_only_contract(self) -> None:
        tools = await self._tools()
        self.assertEqual(
            tuple(tools),
            (
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
                "fs_write",
                "repo_observer",
                "repo_diff",
                "repo_stage",
                "repo_commit",
                "repo_fast_forward",
                "repo_publish",
                "screen_capture",
                "runtime_capabilities",
            ),
        )
        tool = tools["repo_observer"]
        self.assertEqual(_annotation_tuple(tool), (True, False, True, False))
        self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertEqual(set(tool.input_schema["properties"]), {"cwd", "max_paths"})
        max_paths = tool.input_schema["properties"]["max_paths"]
        self.assertEqual(max_paths["minimum"], 1)
        self.assertEqual(max_paths["maximum"], 1000)
        self.assertEqual(max_paths["default"], 200)
        self.assertNotIn("argv", tool.input_schema["properties"])
        self.assertNotIn("command", tool.input_schema["properties"])
        self.assertNotIn("revision", tool.input_schema["properties"])
        self.assertNotIn("remote", tool.input_schema["properties"])
        self.assertNotIn("fetch", tool.input_schema["properties"])
        self.assertIn("repo_observer", ALLOWED_TOOL_NAMES)
        self.assertIn("repo_observer", server.SERVER_INSTRUCTIONS)

    async def test_success_schema_is_fixed_closed_and_validates_real_result(self) -> None:
        tools = await self._tools()
        schema = tools["repo_observer"].output_schema
        self.assertIsNotNone(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]),
            {
                "schema_version",
                "repository",
                "branch",
                "tracking",
                "changes",
                "diff_summary",
                "operation_state",
                "worktrees",
                "observation",
                "truncation",
            },
        )

        result = await server.mcp.call_tool(
            "repo_observer",
            {"cwd": str(ROOT), "max_paths": 200},
        )
        self.assertFalse(result.is_error, result)
        Draft202012Validator(schema).validate(result.structured_content)
        self.assertFalse(result.structured_content["observation"]["fetched"])
        self.assertFalse(result.structured_content["observation"]["network_used"])

    async def test_expected_failure_has_structured_error_and_mcp_is_error(self) -> None:
        result = await server.mcp.call_tool(
            "repo_observer",
            {"cwd": "/", "max_paths": 200},
        )
        self.assertTrue(result.is_error, result)
        self.assertEqual(set(result.structured_content), {"error"})
        error = result.structured_content["error"]
        self.assertEqual(set(error), {"code", "message", "retryable"})
        self.assertEqual(error["code"], "OUTSIDE_WORKSPACE")
        self.assertIs(error["retryable"], False)

    async def test_generated_input_rejects_extra_fields_and_invalid_max_paths(self) -> None:
        for arguments in (
            {"cwd": str(ROOT), "max_paths": 0},
            {"cwd": str(ROOT), "max_paths": 1001},
            {"cwd": str(ROOT), "max_paths": 20, "fetch": True},
            {"cwd": str(ROOT), "max_paths": 20, "revision": "HEAD"},
        ):
            with self.subTest(arguments=arguments):
                try:
                    result = await server.mcp.call_tool("repo_observer", arguments)
                except Exception:
                    continue
                self.assertTrue(result.is_error, result)
