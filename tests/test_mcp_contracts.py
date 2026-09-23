from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server
from agent_runtime.version import RUNTIME_VERSION

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
    "repo_stage": (False, True, False, False),
    "repo_commit": (False, True, False, False),
    "repo_fast_forward": (False, True, True, True),
    "repo_publish": (False, True, True, True),
    "screen_capture": (True, False, True, False),
    "runtime_capabilities": (True, False, True, False),
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


def _string_branch(schema: dict[str, object]) -> dict[str, object]:
    if schema.get("type") == "string":
        return schema
    for candidate in schema.get("anyOf", []):
        if isinstance(candidate, dict) and candidate.get("type") == "string":
            return candidate
    raise AssertionError(f"no string branch in {schema!r}")


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

    async def test_exact_public_tool_surface_and_annotations_are_preserved(self) -> None:
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
        self.assertNotIn("pattern", exec_props["cwd"])
        self.assertEqual(exec_props["timeout_seconds"]["exclusiveMinimum"], 0)
        self.assertEqual(exec_props["timeout_seconds"]["maximum"], 3600)
        self.assertEqual(exec_props["timeout_seconds"]["default"], 300)

        start_props = tools["terminal_start"].input_schema["properties"]
        self.assertEqual(start_props["argv"]["minItems"], 1)
        self.assertEqual(start_props["cwd"]["minLength"], 1)
        self.assertNotIn("pattern", start_props["cwd"])
        start_identity = _string_branch(start_props["start_identity"])
        self.assertEqual(start_identity["minLength"], 32)
        self.assertEqual(start_identity["maxLength"], 32)
        self.assertEqual(start_identity["pattern"], "^[0-9a-f]{32}$")

        poll_props = tools["terminal_poll"].input_schema["properties"]
        self.assertEqual(_string_branch(poll_props["session_id"])["minLength"], 1)
        poll_identity = _string_branch(poll_props["start_identity"])
        self.assertEqual(poll_identity["minLength"], 32)
        self.assertEqual(poll_identity["maxLength"], 32)
        self.assertEqual(poll_identity["pattern"], "^[0-9a-f]{32}$")
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

        fast_forward_props = tools["repo_fast_forward"].input_schema["properties"]
        self.assertEqual(
            set(fast_forward_props),
            {"cwd", "branch", "expected_local_head", "expected_remote_head"},
        )
        self.assertNotIn("pattern", fast_forward_props["cwd"])
        self.assertEqual(fast_forward_props["branch"]["minLength"], 1)
        self.assertLessEqual(fast_forward_props["branch"]["maxLength"], 255)
        for name in ("expected_local_head", "expected_remote_head"):
            self.assertEqual(fast_forward_props[name]["minLength"], 40)
            self.assertEqual(fast_forward_props[name]["maxLength"], 40)
            self.assertEqual(fast_forward_props[name]["pattern"], "^[0-9a-f]{40}$")

        publish_props = tools["repo_publish"].input_schema["properties"]
        self.assertEqual(
            set(publish_props),
            {"cwd", "branch", "expected_remote_head", "commit"},
        )
        self.assertNotIn("pattern", publish_props["cwd"])
        self.assertEqual(publish_props["branch"]["minLength"], 1)
        self.assertLessEqual(publish_props["branch"]["maxLength"], 255)
        for name in ("expected_remote_head", "commit"):
            self.assertEqual(publish_props[name]["minLength"], 40)
            self.assertEqual(publish_props[name]["maxLength"], 40)
            self.assertEqual(publish_props[name]["pattern"], "^[0-9a-f]{40}$")

    async def test_task0078_cwd_schemas_are_connector_portable(self) -> None:
        tools = await self._tools()
        for name in ("terminal_exec", "terminal_start", "fs_read_batch", "repo_fast_forward", "repo_publish"):
            with self.subTest(tool=name):
                cwd_schema = tools[name].input_schema["properties"]["cwd"]
                self.assertEqual(cwd_schema["minLength"], 1)
                self.assertNotIn("pattern", cwd_schema)

    async def test_task0078_relative_cwd_reaches_runtime_fail_closed_validation(self) -> None:
        cases = (
            ("terminal_exec", {"argv": ["/usr/bin/true"], "cwd": "relative"}),
            ("terminal_start", {"argv": ["/usr/bin/true"], "cwd": "relative"}),
            ("fs_read_batch", {"cwd": "relative", "items": [{"path": "README.md"}]}),
        )
        for name, arguments in cases:
            with self.subTest(tool=name):
                with self.assertRaisesRegex(Exception, "cwd must be an absolute path"):
                    await server.mcp.call_tool(name, arguments)

    async def test_task0078_outside_workspace_cwd_remains_rejected(self) -> None:
        cases = (
            ("terminal_exec", {"argv": ["/usr/bin/true"], "cwd": "/"}),
            ("terminal_start", {"argv": ["/usr/bin/true"], "cwd": "/"}),
            ("fs_read_batch", {"cwd": "/", "items": [{"path": "README.md"}]}),
        )
        for name, arguments in cases:
            with self.subTest(tool=name):
                with self.assertRaisesRegex(Exception, "outside AGENT_RUNTIME_WORKSPACE_ROOT"):
                    await server.mcp.call_tool(name, arguments)

    async def test_task0078_resize_contract_is_truthfully_safe_and_bounded(self) -> None:
        tools = await self._tools()
        resize = tools["terminal_resize"]
        self.assertEqual(_annotation_tuple(resize), (False, False, True, False))
        resize_props = resize.input_schema["properties"]
        self.assertEqual(set(resize_props), {"session_id", "rows", "cols"})
        for field in ("rows", "cols"):
            integer = _integer_branch(resize_props[field])
            self.assertEqual(integer["minimum"], 1)
            self.assertEqual(integer["maximum"], 65535)

    async def test_task0078_destructive_control_keeps_write_interrupt_terminate_only(self) -> None:
        tools = await self._tools()
        control = tools["terminal_control"]
        self.assertEqual(_annotation_tuple(control), (False, True, False, True))
        control_props = control.input_schema["properties"]
        self.assertEqual(control_props["action"]["enum"], ["write", "interrupt", "terminate"])
        self.assertNotIn("rows", control_props)
        self.assertNotIn("cols", control_props)

    async def test_task0078_resize_capability_remains_functional(self) -> None:
        start = await server.mcp.call_tool(
            "terminal_start", {"argv": ["/bin/cat"], "cwd": str(ROOT)}
        )
        self.assertFalse(start.is_error)
        session_id = start.structured_content["session_id"]
        terminated = False
        try:
            resized = await server.mcp.call_tool(
                "terminal_resize",
                {"session_id": session_id, "rows": 31, "cols": 101},
            )
            self.assertFalse(resized.is_error)
            self.assertEqual(resized.structured_content["status"], "running")
            ended = await server.mcp.call_tool(
                "terminal_control",
                {"session_id": session_id, "action": "terminate"},
            )
            self.assertFalse(ended.is_error)
            terminated = True
        finally:
            if not terminated:
                try:
                    await server.mcp.call_tool(
                        "terminal_control",
                        {"session_id": session_id, "action": "terminate"},
                    )
                except Exception:
                    pass

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
                "start_identity",
                "status",
                "lifecycle",
                "termination_reason",
                "output",
                "next_cursor",
                "cursor_expired",
                "dropped_output_bytes",
                "exit_code",
            },
            "terminal_poll": {
                "session_id",
                "start_identity",
                "status",
                "lifecycle",
                "termination_reason",
                "output",
                "next_cursor",
                "cursor_expired",
                "dropped_output_bytes",
                "exit_code",
            },
            "terminal_control": {"session_id", "status", "exit_code"},
            "terminal_resize": {"session_id", "status", "exit_code"},
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
            "fs_read_batch": {
                "items", "status", "path", "start_line", "end_line",
                "text", "error_code", "message",
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
            "repo_stage": {
                "schema_version", "branch", "head_sha", "staged_paths",
                "staged_diff_receipt", "post_stage_clean", "network_used",
                "path", "operation", "worktree_sha256", "git_blob_sha",
                "git_mode", "kind", "digest",
            },
            "repo_commit": {
                "schema_version", "branch", "parent_sha", "tree_sha", "commit_sha",
                "diff_receipt", "commit_receipt", "network_used",
                "post_commit_clean", "kind", "digest",
            },
            "repo_fast_forward": {
                "schema_version", "status", "repository_root", "branch", "remote",
                "upstream", "expected_local_head", "expected_remote_head", "head_before",
                "head_after", "tracking_head", "fetched", "network_used",
                "fast_forwarded", "deadline_seconds",
            },
            "repo_publish": {
                "schema_version", "status", "repository_root", "branch", "remote",
                "upstream", "expected_remote_head", "commit", "head",
                "remote_head_before", "remote_head_after", "network_used",
                "push_attempted", "published", "deadline_seconds",
            },
        }
        self.assertIsNone(tools["screen_capture"].output_schema)
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
        self.assertEqual(server.mcp.version, RUNTIME_VERSION)
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
            "terminal_resize",
            "bounded output",
            "capacity_observer",
            "read-only advisory",
            "repo_observer",
            "local-only Git",
            "repo_fast_forward",
            "repo_publish",
            "screen_capture",
            "visual perception is governance-blocked",
            "VISUAL_PERCEPTION_BLOCKED",
            "before native capture",
            "future Architect re-authorization",
            "new source verification, packaging, and activation",
            "fixed-origin",
            "publication",
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

            resize_result = await server.mcp.call_tool(
                "terminal_resize",
                {"session_id": session_id, "rows": 30, "cols": 100},
            )
            self.assertFalse(resize_result.is_error)
            Draft202012Validator(tools["terminal_resize"].output_schema).validate(
                resize_result.structured_content
            )

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

        repo_result = await server.mcp.call_tool(
            "repo_observer",
            {"cwd": str(ROOT), "max_paths": 200},
        )
        self.assertFalse(repo_result.is_error)
        Draft202012Validator(tools["repo_observer"].output_schema).validate(
            repo_result.structured_content
        )
        self.assertFalse(repo_result.structured_content["observation"]["fetched"])
        self.assertFalse(repo_result.structured_content["observation"]["network_used"])

    async def test_invalid_boundary_values_are_rejected_before_tool_bodies_run(self) -> None:
        cases = (
            ("terminal_exec", "execute_terminal", {"argv": ["/usr/bin/true"], "cwd": str(ROOT), "timeout_seconds": 0}),
            ("terminal_exec", "execute_terminal", {"argv": ["/usr/bin/true"], "cwd": str(ROOT), "timeout_seconds": 3600.1}),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": -1, "wait_ms": 0}),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": 0, "wait_ms": -1}),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": 0, "wait_ms": 1001}),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "invalid"}),
            ("terminal_resize", "_control_terminal", {"session_id": "session", "rows": 0, "cols": 24}),
            ("terminal_resize", "_control_terminal", {"session_id": "session", "rows": 65536, "cols": 24}),
            ("terminal_resize", "_control_terminal", {"session_id": "session", "rows": 24, "cols": 0}),
            ("terminal_resize", "_control_terminal", {"session_id": "session", "rows": 24, "cols": 65536}),
            ("repo_observer", "observe_repository", {"cwd": str(ROOT), "max_paths": 0}),
            ("repo_observer", "observe_repository", {"cwd": str(ROOT), "max_paths": 1001}),
        )
        for tool_name, delegate_name, arguments in cases:
            with self.subTest(tool=tool_name, arguments=arguments):
                with patch.object(server, delegate_name) as delegate:
                    await self._assert_sdk_rejects(tool_name, arguments)
                    delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
