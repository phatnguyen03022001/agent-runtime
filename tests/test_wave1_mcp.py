from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server
from agent_runtime.fs_list import FS_LIST_CONTRACT
from agent_runtime.fs_patch import FS_PATCH_CONTRACT
from agent_runtime.fs_search import FS_SEARCH_CONTRACT
from agent_runtime.repo_diff import REPO_DIFF_CONTRACT

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
    "repo_fast_forward",
    "repo_publish",
    "screen_capture",
)


def _annotation_tuple(tool: object) -> tuple[bool, bool, bool, bool]:
    values = tool.annotations.model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


def _contract_annotations(contract: object) -> tuple[bool, bool, bool, bool]:
    annotations = contract.annotations
    return (
        annotations.read_only,
        annotations.destructive,
        annotations.idempotent,
        annotations.open_world,
    )


def _walk_schema(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema(child)


class Wave1MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root),
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    async def test_exact_16_tool_surface_and_contract_sourced_annotations(self) -> None:
        listed = await server.mcp.list_tools()
        self.assertEqual(tuple(tool.name for tool in listed), EXPECTED_TOOLS)
        self.assertEqual(server.PUBLIC_TOOL_NAMES, EXPECTED_TOOLS)
        tools = {tool.name: tool for tool in listed}
        expected = {
            "fs_list": FS_LIST_CONTRACT,
            "fs_search": FS_SEARCH_CONTRACT,
            "fs_patch": FS_PATCH_CONTRACT,
            "repo_diff": REPO_DIFF_CONTRACT,
        }
        for name, contract in expected.items():
            self.assertEqual(_annotation_tuple(tools[name]), _contract_annotations(contract))

    async def test_new_input_and_output_schemas_are_closed_and_bounded(self) -> None:
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        for name in ("fs_list", "fs_search", "fs_patch", "repo_diff"):
            input_schema = tools[name].input_schema
            self.assertIs(input_schema["additionalProperties"], False, name)
            output_schema = tools[name].output_schema
            self.assertIsNotNone(output_schema, name)
            for node in _walk_schema(output_schema):
                if node.get("type") == "object" and "properties" in node:
                    self.assertIs(node.get("additionalProperties"), False, (name, node))

        fs_list = tools["fs_list"].input_schema["properties"]
        self.assertEqual(fs_list["path"]["default"], ".")
        self.assertEqual(fs_list["path"]["maxLength"], 4096)
        self.assertEqual(fs_list["max_entries"]["minimum"], 1)
        self.assertEqual(fs_list["max_entries"]["maximum"], 1000)
        self.assertEqual(fs_list["max_entries"]["default"], 200)

        fs_search = tools["fs_search"].input_schema["properties"]
        self.assertEqual(set(fs_search["mode"]["enum"]), {"content", "path"})
        self.assertEqual(fs_search["query"]["minLength"], 1)
        self.assertEqual(fs_search["query"]["maxLength"], 4096)
        self.assertEqual(fs_search["root_path"]["default"], ".")
        self.assertEqual(fs_search["root_path"]["maxLength"], 4096)
        self.assertEqual(fs_search["max_results"]["minimum"], 1)
        self.assertEqual(fs_search["max_results"]["maximum"], 500)
        self.assertEqual(fs_search["max_results"]["default"], 100)

        fs_patch = tools["fs_patch"].input_schema
        self.assertEqual(
            set(fs_patch["required"]),
            {"cwd", "path", "expected_sha256", "edits"},
        )
        props = fs_patch["properties"]
        self.assertEqual(props["expected_sha256"]["pattern"], "^[0-9a-f]{64}$")
        self.assertEqual(props["edits"]["minItems"], 1)
        self.assertEqual(props["edits"]["maxItems"], 20)
        edit_schema = fs_patch["$defs"]["FsPatchEdit"]
        self.assertIs(edit_schema["additionalProperties"], False)

        repo_diff = tools["repo_diff"].input_schema["properties"]
        self.assertEqual(set(repo_diff["scope"]["enum"]), {"worktree", "staged"})
        self.assertEqual(repo_diff["scope"]["default"], "worktree")

    async def test_capability_failure_is_structured_generic_error_envelope(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        result = await server.mcp.call_tool(
            "fs_list",
            {"cwd": outside.name, "path": ".", "max_entries": 10},
        )
        self.assertTrue(getattr(result, "is_error", False), result)
        structured = getattr(result, "structured_content", None)
        self.assertIsInstance(structured, dict)
        self.assertEqual(set(structured), {"error"})
        error = structured["error"]
        self.assertEqual(
            set(error),
            {"code", "reason_code", "message", "retryable"},
        )
        self.assertEqual(error["code"], "OUTSIDE_WORKSPACE")
        self.assertEqual(error["reason_code"], "CWD_OUTSIDE_WORKSPACE")
        self.assertIs(error["retryable"], False)
        self.assertLessEqual(len(error["message"]), 256)

    async def test_fs_search_sha_is_directly_usable_as_fs_patch_expected_sha(self) -> None:
        raw = b"alpha needle\n"
        target = self.root / "target.txt"
        target.write_bytes(raw)
        search_result = await server.mcp.call_tool(
            "fs_search",
            {
                "cwd": str(self.root),
                "query": "needle",
                "mode": "content",
            },
        )
        self.assertFalse(getattr(search_result, "is_error", False), search_result)
        search_payload = getattr(search_result, "structured_content")
        digest = search_payload["results"][0]["file_sha256"]
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

        patch_result = await server.mcp.call_tool(
            "fs_patch",
            {
                "cwd": str(self.root),
                "path": "target.txt",
                "expected_sha256": digest,
                "edits": [{"old_text": "needle", "new_text": "patched"}],
            },
        )
        self.assertFalse(getattr(patch_result, "is_error", False), patch_result)
        Draft202012Validator(
            {tool.name: tool for tool in await server.mcp.list_tools()}["fs_patch"].output_schema
        ).validate(getattr(patch_result, "structured_content"))
        self.assertEqual(target.read_bytes(), b"alpha patched\n")

    async def test_screen_capture_governance_guard_remains_before_native_capture(self) -> None:
        with patch(
            "agent_runtime.server.capture_screen",
            side_effect=AssertionError("native capture must not run"),
        ):
            result = await server.mcp.call_tool("screen_capture", {})
        self.assertTrue(getattr(result, "is_error", False), result)
        text = "\n".join(
            getattr(block, "text", "")
            for block in getattr(result, "content", [])
        )
        self.assertIn("VISUAL_PERCEPTION_BLOCKED", text)


if __name__ == "__main__":
    unittest.main()
