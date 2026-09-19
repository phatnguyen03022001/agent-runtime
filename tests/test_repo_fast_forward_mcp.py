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
    "repo_observer",
    "repo_fast_forward",
)
SUCCESS_FIELDS = {
    "schema_version",
    "status",
    "repository_root",
    "branch",
    "remote",
    "upstream",
    "expected_local_head",
    "expected_remote_head",
    "head_before",
    "head_after",
    "tracking_head",
    "fetched",
    "network_used",
    "fast_forwarded",
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


class RepoFastForwardMCPTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_repo_fast_forward_is_public_tool_9_with_exact_annotations(self) -> None:
        tools = await self._tools()
        self.assertEqual(tuple(tools), EXPECTED_TOOLS)
        tool = tools["repo_fast_forward"]
        self.assertEqual(_annotation_tuple(tool), (False, True, True, True))

    async def test_input_schema_is_closed_and_has_only_bound_expected_state(self) -> None:
        tool = (await self._tools())["repo_fast_forward"]
        schema = tool.input_schema
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]),
            {"cwd", "branch", "expected_local_head", "expected_remote_head"},
        )
        self.assertEqual(
            set(schema["required"]),
            {"cwd", "branch", "expected_local_head", "expected_remote_head"},
        )
        self.assertNotIn("pattern", schema["properties"]["cwd"])
        self.assertEqual(schema["properties"]["branch"]["minLength"], 1)
        self.assertLessEqual(schema["properties"]["branch"]["maxLength"], 255)
        for name in ("expected_local_head", "expected_remote_head"):
            sha = schema["properties"][name]
            self.assertEqual(sha["minLength"], 40)
            self.assertEqual(sha["maxLength"], 40)
            self.assertEqual(sha["pattern"], "^[0-9a-f]{40}$")
        for forbidden in (
            "remote",
            "url",
            "refspec",
            "argv",
            "command",
            "timeout",
            "timeout_seconds",
            "force",
            "prune",
            "tags",
            "submodules",
            "merge",
        ):
            self.assertNotIn(forbidden, schema["properties"])

    async def test_success_schema_is_fixed_closed(self) -> None:
        tool = (await self._tools())["repo_fast_forward"]
        schema = tool.output_schema
        self.assertIsNotNone(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), SUCCESS_FIELDS)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            set(schema["properties"]["status"]["enum"]),
            {"fast_forwarded", "already_at_target"},
        )
        self.assertEqual(schema["properties"]["remote"]["const"], "origin")
        self.assertEqual(schema["properties"]["fetched"]["const"], True)
        self.assertEqual(schema["properties"]["network_used"]["const"], True)
        Draft202012Validator.check_schema(schema)

    async def test_timing_server_instructions_and_observer_contract_are_preserved(self) -> None:
        tools = await self._tools()
        self.assertIn("repo_fast_forward", ALLOWED_TOOL_NAMES)
        self.assertIn("repo_fast_forward", server.SERVER_INSTRUCTIONS)
        self.assertIn("fixed origin", server.SERVER_INSTRUCTIONS)
        observer = tools["repo_observer"]
        self.assertEqual(_annotation_tuple(observer), (True, False, True, False))
        self.assertEqual(
            set(observer.input_schema["properties"]),
            {"cwd", "max_paths"},
        )
        self.assertIn("local-only", observer.description.lower())

    async def test_invalid_inputs_and_failures_are_bounded_typed_errors(self) -> None:
        tool = (await self._tools())["repo_fast_forward"]
        for arguments in (
            {
                "cwd": str(ROOT),
                "branch": "dev",
                "expected_local_head": "A" * 40,
                "expected_remote_head": "b" * 40,
            },
            {
                "cwd": str(ROOT),
                "branch": "dev",
                "expected_local_head": "a" * 40,
                "expected_remote_head": "b" * 40,
                "remote": "upstream",
            },
        ):
            with self.subTest(arguments=arguments):
                try:
                    result = await server.mcp.call_tool("repo_fast_forward", arguments)
                except Exception:
                    continue
                self.assertTrue(result.is_error, result)

        current = os.popen(f"/usr/bin/git -C {ROOT} rev-parse HEAD").read().strip()
        result = await server.mcp.call_tool(
            "repo_fast_forward",
            {
                "cwd": "/",
                "branch": "dev",
                "expected_local_head": current,
                "expected_remote_head": current,
            },
        )
        self.assertTrue(result.is_error, result)
        self.assertEqual(set(result.structured_content), {"error"})
        self.assertEqual(
            set(result.structured_content["error"]),
            {"code", "message", "retryable"},
        )
        self.assertEqual(result.structured_content["error"]["code"], "OUTSIDE_WORKSPACE")
        self.assertLessEqual(len(result.structured_content["error"]["message"]), 256)

    def test_readme_documents_exact_nine_tool_surface_and_typed_git_roles(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("exactly nine public tools", readme)
        self.assertIn("repo_observer", readme)
        self.assertIn("typed local-only read-only Git observation", readme)
        self.assertIn("repo_fast_forward", readme)
        self.assertIn("expected-state-guarded fixed-origin synchronization", readme)


if __name__ == "__main__":
    unittest.main()
