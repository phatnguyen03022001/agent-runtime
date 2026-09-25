from __future__ import annotations

import hashlib
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server
from agent_runtime.fs_write import (
    FS_WRITE_CONTRACT,
    MAX_CONTENT_BYTES,
    MAX_INPUT_FILE_BYTES,
    MAX_OUTPUT_FILE_BYTES,
)
from agent_runtime.tool_contract import (
    Authority,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
)

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
    "fs_manage",
    "repo_observer",
    "repo_remote_observer",
    "repo_diff",
    "repo_stage",
    "repo_commit",
    "repo_fast_forward",
    "repo_publish",
    "runtime_capabilities",
)

LEGACY_ANNOTATIONS = {
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
    "repo_observer": (True, False, True, False),
    "repo_remote_observer": (True, False, True, True),
    "repo_diff": (True, False, True, False),
    "repo_stage": (False, True, False, False),
    "repo_commit": (False, True, False, False),
    "repo_fast_forward": (False, True, True, True),
    "repo_publish": (False, True, True, True),
}


def _annotations(tool: object) -> tuple[bool, bool, bool, bool]:
    values = tool.annotations.model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class FsWriteMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root),
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    async def _tools(self) -> dict[str, object]:
        return {tool.name: tool for tool in await server.mcp.list_tools()}

    async def test_exact_18_tool_order_and_fs_write_annotations(self) -> None:
        tools = await self._tools()
        self.assertEqual(tuple(tools), EXPECTED_TOOLS)
        self.assertEqual(server.PUBLIC_TOOL_NAMES, EXPECTED_TOOLS)
        self.assertEqual(_annotations(tools["fs_write"]), (False, True, False, False))

    async def test_existing_15_annotations_remain_compatible(self) -> None:
        tools = await self._tools()
        for name, expected in LEGACY_ANNOTATIONS.items():
            with self.subTest(name=name):
                self.assertEqual(_annotations(tools[name]), expected)

    async def test_fs_write_contract_matches_authority_annotations_and_bounds(self) -> None:
        self.assertEqual(FS_WRITE_CONTRACT.name, "fs_write")
        self.assertIs(FS_WRITE_CONTRACT.tool_class, ToolClass.WRITE)
        self.assertEqual(
            FS_WRITE_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
        )
        self.assertEqual(
            FS_WRITE_CONTRACT.annotations,
            ToolAnnotations(False, True, False, False),
        )
        self.assertEqual(FS_WRITE_CONTRACT.bounds["content_utf8_bytes"], MAX_CONTENT_BYTES)
        self.assertEqual(
            FS_WRITE_CONTRACT.bounds["replace_input_file_bytes"],
            MAX_INPUT_FILE_BYTES,
        )
        self.assertEqual(
            FS_WRITE_CONTRACT.bounds["output_file_bytes"],
            MAX_OUTPUT_FILE_BYTES,
        )
        self.assertEqual(FS_WRITE_CONTRACT.postconditions["receipt_kind"], "fs-write")

    async def test_input_schema_is_closed_and_exact(self) -> None:
        schema = (await self._tools())["fs_write"].input_schema
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]),
            {"cwd", "path", "operation", "content", "expected_sha256"},
        )
        self.assertEqual(
            set(schema["required"]),
            {"cwd", "path", "operation", "content"},
        )
        props = schema["properties"]
        self.assertEqual(props["path"]["maxLength"], 4096)
        self.assertEqual(set(props["operation"]["enum"]), {"create", "replace"})
        self.assertEqual(props["content"]["maxLength"], 1024 * 1024)
        sha_branches = [
            item
            for item in props["expected_sha256"]["anyOf"]
            if item.get("type") == "string"
        ]
        self.assertEqual(len(sha_branches), 1)
        self.assertEqual(sha_branches[0]["pattern"], "^[0-9a-f]{64}$")
        self.assertIsNone(props["expected_sha256"]["default"])

    async def test_output_schema_is_closed_and_exact(self) -> None:
        schema = (await self._tools())["fs_write"].output_schema
        self.assertIsNotNone(schema)
        Draft202012Validator.check_schema(schema)
        for node in _walk(schema):
            if node.get("type") == "object" and "properties" in node:
                self.assertIs(node.get("additionalProperties"), False, node)
        self.assertEqual(
            set(schema["properties"]),
            {
                "schema_version",
                "status",
                "path",
                "sha256_before",
                "sha256_after",
                "bytes_before",
                "bytes_after",
                "mode_before",
                "mode_after",
                "write_receipt",
            },
        )
        receipt = schema["$defs"]["FsWriteReceiptResult"]
        self.assertEqual(receipt["properties"]["kind"]["const"], "fs-write")

    async def test_create_expected_sha_semantics_return_capability_envelope(self) -> None:
        result = await server.mcp.call_tool(
            "fs_write",
            {
                "cwd": str(self.root),
                "path": "x.txt",
                "operation": "create",
                "content": "x",
                "expected_sha256": "0" * 64,
            },
        )
        self.assertTrue(result.is_error)
        self.assertEqual(set(result.structured_content), {"error"})
        error = result.structured_content["error"]
        self.assertEqual(
            set(error),
            {"code", "reason_code", "message", "retryable", "effect_state", "reconciliation_required", "safe_next_action"},
        )
        self.assertEqual(error["code"], "INVALID_ARGUMENT")
        self.assertEqual(error["reason_code"], "EXPECTED_SHA_FORBIDDEN")
        self.assertFalse((self.root / "x.txt").exists())

    async def test_replace_expected_sha_is_mandatory_at_runtime(self) -> None:
        target = self.root / "x.txt"
        target.write_text("old")
        result = await server.mcp.call_tool(
            "fs_write",
            {
                "cwd": str(self.root),
                "path": "x.txt",
                "operation": "replace",
                "content": "new",
            },
        )
        self.assertTrue(result.is_error)
        self.assertEqual(
            result.structured_content["error"]["reason_code"],
            "EXPECTED_SHA_REQUIRED",
        )
        self.assertEqual(target.read_text(), "old")

    async def test_mcp_create_and_replace_success_validate_declared_schema(self) -> None:
        tool = (await self._tools())["fs_write"]
        created = await server.mcp.call_tool(
            "fs_write",
            {
                "cwd": str(self.root),
                "path": "x.txt",
                "operation": "create",
                "content": "old",
            },
        )
        self.assertFalse(created.is_error, created)
        Draft202012Validator(tool.output_schema).validate(created.structured_content)
        self.assertEqual(created.structured_content["status"], "created")
        self.assertEqual(created.structured_content["write_receipt"]["kind"], "fs-write")

        old_sha = hashlib.sha256(b"old").hexdigest()
        replaced = await server.mcp.call_tool(
            "fs_write",
            {
                "cwd": str(self.root),
                "path": "x.txt",
                "operation": "replace",
                "content": "new",
                "expected_sha256": old_sha,
            },
        )
        self.assertFalse(replaced.is_error, replaced)
        Draft202012Validator(tool.output_schema).validate(replaced.structured_content)
        self.assertEqual(replaced.structured_content["status"], "replaced")
        self.assertEqual((self.root / "x.txt").read_text(), "new")

    async def test_mcp_replay_failures_are_structured_and_non_mutating(self) -> None:
        first = await server.mcp.call_tool(
            "fs_write",
            {
                "cwd": str(self.root),
                "path": "x.txt",
                "operation": "create",
                "content": "old",
            },
        )
        self.assertFalse(first.is_error)
        replay = await server.mcp.call_tool(
            "fs_write",
            {
                "cwd": str(self.root),
                "path": "x.txt",
                "operation": "create",
                "content": "new",
            },
        )
        self.assertTrue(replay.is_error)
        self.assertEqual(
            replay.structured_content["error"]["reason_code"],
            "TARGET_ALREADY_EXISTS",
        )
        self.assertEqual((self.root / "x.txt").read_text(), "old")

    async def test_fs_write_signature_contains_only_public_fields(self) -> None:
        self.assertEqual(
            list(inspect.signature(server.fs_write).parameters),
            ["cwd", "path", "operation", "content", "expected_sha256"],
        )

    async def test_screen_capture_guard_remains_before_native_capture(self) -> None:
        with patch(
            "agent_runtime.server.capture_screen",
            side_effect=AssertionError("native capture must not run"),
        ):
            result = server.screen_capture()
        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(error["reason_code"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertEqual(error["effect_state"], "absent")
        self.assertEqual(error["safe_next_action"], "unsupported")


if __name__ == "__main__":
    unittest.main()
