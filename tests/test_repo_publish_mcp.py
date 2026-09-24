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
EXPECTED_TOOLS = (
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
)
EXPECTED_ANNOTATIONS = {
    "terminal_exec": (False, True, True, True),
    "terminal_start": (False, True, False, True),
    "terminal_poll": (False, False, False, False),
    "terminal_control": (False, True, False, True),
    "terminal_resize": (False, False, True, False),
    "capacity_observer": (True, False, True, False),
    "fs_read_batch": (True, False, True, False),
    "fs_list": (True, False, True, False),
    "fs_search": (True, False, True, False),
    "fs_patch": (False, True, False, False),
    "fs_write": (False, True, False, False),
    "repo_observer": (True, False, True, False),
    "repo_diff": (True, False, True, False),
    "repo_stage": (False, True, False, False),
    "repo_commit": (False, True, False, False),
    "repo_fast_forward": (False, True, True, True),
    "repo_publish": (False, True, True, True),
    "screen_capture": (True, False, True, False),
    "runtime_capabilities": (True, False, True, False),
}
SUCCESS_FIELDS = {
    "schema_version",
    "status",
    "repository_root",
    "branch",
    "remote",
    "upstream",
    "expected_remote_head",
    "commit",
    "head",
    "remote_head_before",
    "remote_head_after",
    "network_used",
    "push_attempted",
    "published",
    "deadline_seconds",
}


def _annotation_tuple(tool: object) -> tuple[bool, bool, bool, bool]:
    values = getattr(tool, "annotations").model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


class RepoPublishMCPTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_repo_publish_is_public_tool_17(self) -> None:
        tools = await self._tools()
        self.assertEqual(tuple(tools), EXPECTED_TOOLS)
        for name, tool in tools.items():
            self.assertEqual(_annotation_tuple(tool), EXPECTED_ANNOTATIONS[name])
        self.assertEqual(_annotation_tuple(tools["repo_publish"]), (False, True, True, True))

    async def test_input_schema_is_closed_and_has_only_publication_binding(self) -> None:
        tool = (await self._tools())["repo_publish"]
        schema = tool.input_schema
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]),
            {"cwd", "branch", "expected_remote_head", "commit"},
        )
        self.assertEqual(
            set(schema["required"]),
            {"cwd", "branch", "expected_remote_head", "commit"},
        )
        self.assertNotIn("pattern", schema["properties"]["cwd"])
        self.assertEqual(schema["properties"]["branch"]["minLength"], 1)
        self.assertLessEqual(schema["properties"]["branch"]["maxLength"], 255)
        for name in ("expected_remote_head", "commit"):
            sha = schema["properties"][name]
            self.assertEqual(sha["minLength"], 40)
            self.assertEqual(sha["maxLength"], 40)
            self.assertEqual(sha["pattern"], "^[0-9a-f]{40}$")
        for forbidden in (
            "remote", "url", "refspec", "argv", "command", "timeout",
            "timeout_seconds", "force", "force_with_lease", "atomic", "tags",
            "push_options", "recurse_submodules", "receive_pack", "upload_pack",
            "config", "environment",
        ):
            self.assertNotIn(forbidden, schema["properties"])

    async def test_success_schema_is_fixed_closed(self) -> None:
        schema = (await self._tools())["repo_publish"].output_schema
        self.assertIsNotNone(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), SUCCESS_FIELDS)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            set(schema["properties"]["status"]["enum"]),
            {"published", "already_published"},
        )
        self.assertEqual(schema["properties"]["remote"]["const"], "origin")
        self.assertEqual(schema["properties"]["network_used"]["const"], True)
        Draft202012Validator.check_schema(schema)

    async def test_legacy_relative_order_and_wave1_insertions_are_preserved(self) -> None:
        tools = await self._tools()
        legacy_order = (
            "terminal_exec",
            "terminal_start",
            "terminal_poll",
            "terminal_control",
            "terminal_resize",
            "capacity_observer",
            "fs_read_batch",
            "repo_observer",
            "repo_fast_forward",
            "repo_publish",
            "screen_capture",
        )
        self.assertEqual(
            tuple(name for name in tools if name in legacy_order),
            legacy_order,
        )
        self.assertEqual(tuple(tools)[7:11], ("fs_list", "fs_search", "fs_patch", "fs_write"))
        self.assertEqual(tuple(tools)[12], "repo_diff")
        for name, tool in tools.items():
            self.assertEqual(_annotation_tuple(tool), EXPECTED_ANNOTATIONS[name])
        self.assertIn("repo_publish", ALLOWED_TOOL_NAMES)
        self.assertIn("repo_publish", server.SERVER_INSTRUCTIONS)
        self.assertIn("fixed-origin publication", server.SERVER_INSTRUCTIONS)
        self.assertIn("repository and task authority remain external", server.SERVER_INSTRUCTIONS)

    async def test_invalid_inputs_and_failures_are_bounded_typed_errors(self) -> None:
        for arguments in (
            {
                "cwd": str(ROOT),
                "branch": "dev",
                "expected_remote_head": "A" * 40,
                "commit": "b" * 40,
            },
            {
                "cwd": str(ROOT),
                "branch": "dev",
                "expected_remote_head": "a" * 40,
                "commit": "b" * 40,
                "remote": "upstream",
            },
        ):
            with self.subTest(arguments=arguments):
                try:
                    result = await server.mcp.call_tool("repo_publish", arguments)
                except Exception:
                    continue
                self.assertTrue(result.is_error, result)

        result = await server.mcp.call_tool(
            "repo_publish",
            {
                "cwd": "/",
                "branch": "dev",
                "expected_remote_head": "a" * 40,
                "commit": "b" * 40,
            },
        )
        self.assertTrue(result.is_error, result)
        self.assertEqual(set(result.structured_content), {"error"})
        self.assertEqual(
            set(result.structured_content["error"]),
            {"code", "reason_code", "message", "retryable", "effect_state", "reconciliation_required", "safe_next_action"},
        )
        self.assertEqual(result.structured_content["error"]["code"], "OUTSIDE_WORKSPACE")
        self.assertLessEqual(len(result.structured_content["error"]["message"]), 256)

    def test_readme_documents_exact_eleven_tool_surface_and_publication_boundary(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("exactly nineteen public tools", readme)
        self.assertIn("repo_publish", readme)
        self.assertIn("expected-state-guarded fixed-origin publication", readme)
        self.assertIn("repository/task authority remains", readme)
        self.assertIn("outside Runtime", readme)


if __name__ == "__main__":
    unittest.main()
