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
EXPECTED_ANNOTATIONS = {
    "terminal_exec": (False, True, False, True),
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
    "repo_fast_forward": (False, True, True, True),
    "repo_publish": (False, True, True, True),
    "screen_capture": (True, False, True, False),
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
    "terminal_resize": {"session_id", "status", "exit_code"},
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
    "fs_list": {
        "schema_version", "path", "entries", "name", "kind", "size_bytes",
        "truncated", "scanned_entries", "skipped_invalid_names",
    },
    "fs_search": {
        "schema_version", "results", "path", "line_number", "line_text",
        "line_truncated", "file_sha256", "truncated", "limit_reason",
        "files_scanned", "bytes_scanned", "skipped_invalid_utf8", "skipped_nul",
        "skipped_symlinks",
    },
    "fs_patch": {
        "schema_version", "path", "sha256_before", "sha256_after",
        "bytes_before", "bytes_after", "edits_applied",
    },
    "fs_write": {
        "schema_version", "status", "path", "sha256_before", "sha256_after",
        "bytes_before", "bytes_after", "mode_before", "mode_after",
        "write_receipt", "kind", "digest",
    },
    "repo_observer": {
        "schema_version", "repository", "branch", "tracking", "changes",
        "diff_summary", "operation_state", "worktrees", "observation", "truncation",
        "root", "cwd", "bare", "shallow", "inside_workspace_root",
        "cwd_inside_repo", "cwd_is_repo_root", "head_sha", "name", "detached",
        "upstream", "tracking_sha", "tracking_known", "ahead", "behind",
        "path", "original_path", "index_status", "worktree_status", "tracked",
        "staged", "conflicted", "staged_files", "unstaged_files",
        "untracked_files", "conflicted_files", "additions", "deletions", "exact",
        "merge", "rebase", "cherry_pick", "bisect", "entries",
        "outside_workspace_count", "total_count", "total_exact", "locked",
        "prunable", "fetched", "network_used", "deadline_seconds",
        "changes_truncated", "worktrees_truncated", "diff_truncated",
        "total_changes", "total_changes_exact",
    },
    "repo_diff": {
        "schema_version", "scope", "head_sha", "patch", "patch_truncated",
        "full_diff_bytes", "diff_receipt", "kind", "digest", "network_used",
    },
    "repo_fast_forward": {
        "schema_version", "status", "repository_root", "branch", "remote", "upstream",
        "expected_local_head", "expected_remote_head", "head_before", "head_after",
        "tracking_head", "fetched", "network_used", "fast_forwarded", "deadline_seconds",
    },
    "repo_publish": {
        "schema_version", "status", "repository_root", "branch", "remote", "upstream",
        "expected_remote_head", "commit", "head", "remote_head_before", "remote_head_after",
        "network_used", "push_attempted", "published", "deadline_seconds",
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
            self.assertNotIn("pattern", exec_props["cwd"])
            self.assertEqual(exec_props["timeout_seconds"]["exclusiveMinimum"], 0)
            self.assertEqual(exec_props["timeout_seconds"]["maximum"], 3600)
            self.assertEqual(exec_props["timeout_seconds"]["default"], 300)

            start_props = tools["terminal_start"].input_schema["properties"]
            self.assertEqual(start_props["argv"]["minItems"], 1)
            self.assertEqual(start_props["cwd"]["minLength"], 1)
            self.assertNotIn("pattern", start_props["cwd"])

            poll_props = tools["terminal_poll"].input_schema["properties"]
            self.assertEqual(poll_props["session_id"]["minLength"], 1)
            self.assertEqual(poll_props["cursor"]["minimum"], 0)
            self.assertEqual(poll_props["wait_ms"]["minimum"], 0)
            self.assertEqual(poll_props["wait_ms"]["maximum"], 1000)

            control_props = tools["terminal_control"].input_schema["properties"]
            self.assertEqual(control_props["session_id"]["minLength"], 1)
            self.assertEqual(
                control_props["action"]["enum"],
                ["write", "interrupt", "terminate"],
            )
            self.assertNotIn("rows", control_props)
            self.assertNotIn("cols", control_props)

            resize_props = tools["terminal_resize"].input_schema["properties"]
            self.assertEqual(resize_props["session_id"]["minLength"], 1)
            for field in ("rows", "cols"):
                integer = _integer_branch(resize_props[field])
                self.assertEqual(integer["minimum"], 1)
                self.assertEqual(integer["maximum"], 65535)

            fs_props = tools["fs_read_batch"].input_schema["properties"]
            self.assertNotIn("pattern", fs_props["cwd"])

            repo_props = tools["repo_observer"].input_schema["properties"]
            self.assertEqual(set(repo_props), {"cwd", "max_paths"})
            self.assertEqual(repo_props["max_paths"]["minimum"], 1)
            self.assertEqual(repo_props["max_paths"]["maximum"], 1000)
            self.assertEqual(repo_props["max_paths"]["default"], 200)

            fast_forward_schema = tools["repo_fast_forward"].input_schema
            self.assertIs(fast_forward_schema["additionalProperties"], False)
            self.assertEqual(
                set(fast_forward_schema["properties"]),
                {"cwd", "branch", "expected_local_head", "expected_remote_head"},
            )
            self.assertEqual(
                set(fast_forward_schema["required"]),
                {"cwd", "branch", "expected_local_head", "expected_remote_head"},
            )
            fast_forward_props = fast_forward_schema["properties"]
            self.assertNotIn("pattern", fast_forward_props["cwd"])
            self.assertEqual(fast_forward_props["branch"]["minLength"], 1)
            self.assertLessEqual(fast_forward_props["branch"]["maxLength"], 255)
            for field in ("expected_local_head", "expected_remote_head"):
                sha = fast_forward_props[field]
                self.assertEqual(sha["minLength"], 40)
                self.assertEqual(sha["maxLength"], 40)
                self.assertEqual(sha["pattern"], "^[0-9a-f]{40}$")

            publish_schema = tools["repo_publish"].input_schema
            self.assertIs(publish_schema["additionalProperties"], False)
            self.assertEqual(
                set(publish_schema["properties"]),
                {"cwd", "branch", "expected_remote_head", "commit"},
            )
            self.assertEqual(
                set(publish_schema["required"]),
                {"cwd", "branch", "expected_remote_head", "commit"},
            )
            publish_props = publish_schema["properties"]
            self.assertNotIn("pattern", publish_props["cwd"])
            self.assertEqual(publish_props["branch"]["minLength"], 1)
            self.assertLessEqual(publish_props["branch"]["maxLength"], 255)
            for field in ("expected_remote_head", "commit"):
                sha = publish_props[field]
                self.assertEqual(sha["minLength"], 40)
                self.assertEqual(sha["maxLength"], 40)
                self.assertEqual(sha["pattern"], "^[0-9a-f]{40}$")

            for name, tool in tools.items():
                if name == "screen_capture":
                    self.assertIsNone(tool.output_schema, name)
                    continue
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

            repo_result = await client.call_tool(
                "repo_observer",
                {"cwd": str(ROOT), "max_paths": 200},
            )
            self.assertFalse(repo_result.is_error)
            Draft202012Validator(tools["repo_observer"].output_schema).validate(
                repo_result.structured_content
            )
            self.assertFalse(repo_result.structured_content["observation"]["fetched"])
            self.assertFalse(repo_result.structured_content["observation"]["network_used"])

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

                resize_result = await client.call_tool(
                    "terminal_resize",
                    {"session_id": session_id, "rows": 30, "cols": 100},
                )
                self.assertFalse(resize_result.is_error)
                Draft202012Validator(tools["terminal_resize"].output_schema).validate(
                    resize_result.structured_content
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
