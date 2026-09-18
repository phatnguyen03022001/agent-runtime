from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client
from mcp.types import TextContent

from agent_runtime import fs_read, server
from agent_runtime.contracts import FsReadItem
from agent_runtime.fs_read import read_files_batch

ITEM_SCAN_LIMIT = 1024 * 1024
BATCH_SCAN_LIMIT = 4 * 1024 * 1024
ARGV_ITEM_LIMIT = 16 * 1024
ARGV_TOTAL_LIMIT = 256 * 1024
TERMINAL_DATA_LIMIT = 64 * 1024
MAX_LINE = 2_147_483_647

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent


def _string_branch(schema: dict[str, object]) -> dict[str, object]:
    if schema.get("type") == "string":
        return schema
    for candidate in schema.get("anyOf", []):
        if isinstance(candidate, dict) and candidate.get("type") == "string":
            return candidate
    raise AssertionError(f"no string branch in {schema!r}")


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


class FsReadScanBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-runtime-task0041-scan-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _call(self, *items: FsReadItem) -> list[dict[str, object]]:
        return read_files_batch(str(self.cwd), list(items))["items"]  # type: ignore[index,return-value]

    def _write_two_line_boundary(self, name: str, total_bytes: int) -> None:
        if total_bytes < 3:
            raise AssertionError(total_bytes)
        (self.cwd / name).write_bytes(b"a" * (total_bytes - 3) + b"\n" + b"x\n")

    def test_item_scan_limit_below_at_above_and_later_item_continues(self) -> None:
        self._write_two_line_boundary("below.txt", ITEM_SCAN_LIMIT - 1)
        self._write_two_line_boundary("at.txt", ITEM_SCAN_LIMIT)
        self._write_two_line_boundary("above.txt", ITEM_SCAN_LIMIT + 1)
        (self.cwd / "later.txt").write_bytes(b"later\n")

        results = self._call(
            FsReadItem(path="below.txt", start_line=2, end_line=2),
            FsReadItem(path="at.txt", start_line=2, end_line=2),
            FsReadItem(path="above.txt", start_line=2, end_line=2),
            FsReadItem(path="later.txt"),
        )

        self.assertEqual(results[0]["text"], "x\n")
        self.assertEqual(results[1]["text"], "x\n")
        self.assertEqual(results[2]["error_code"], "ITEM_SCAN_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[2])
        self.assertEqual(results[3]["status"], "ok")
        self.assertEqual(results[3]["text"], "later\n")

    def test_giant_no_newline_and_sparse_range_are_bounded_by_actual_read_bytes(self) -> None:
        (self.cwd / "giant.txt").write_bytes(b"g" * (ITEM_SCAN_LIMIT + 4096))
        (self.cwd / "sparse.txt").write_bytes(b"s\n" * (ITEM_SCAN_LIMIT // 2 + 64))

        original_read = os.read
        original_open_regular = fs_read._open_regular_at
        total_read = 0
        read_totals: list[int] = []
        fd_item_index: dict[int, int] = {}

        def measured_open(cwd_fd: int, components: tuple[str, ...]) -> int:
            fd = original_open_regular(cwd_fd, components)
            fd_item_index[fd] = len(read_totals)
            read_totals.append(0)
            return fd

        def measured_read(fd: int, count: int) -> bytes:
            nonlocal total_read
            item_index = fd_item_index[fd]
            used = read_totals[item_index]
            self.assertGreater(count, 0)
            self.assertLessEqual(count, ITEM_SCAN_LIMIT - used)
            self.assertLessEqual(count, BATCH_SCAN_LIMIT - total_read)
            raw = original_read(fd, count)
            read_totals[item_index] = used + len(raw)
            total_read += len(raw)
            return raw

        with (
            patch.object(fs_read, "_open_regular_at", side_effect=measured_open),
            patch.object(fs_read.os, "read", side_effect=measured_read),
        ):
            results = self._call(
                FsReadItem(path="giant.txt", start_line=2),
                FsReadItem(path="sparse.txt", start_line=ITEM_SCAN_LIMIT),
            )

        self.assertEqual([item["error_code"] for item in results], [
            "ITEM_SCAN_LIMIT_EXCEEDED",
            "ITEM_SCAN_LIMIT_EXCEEDED",
        ])
        self.assertTrue(all("text" not in item for item in results))
        self.assertEqual(total_read, 2 * ITEM_SCAN_LIMIT)
        self.assertEqual(read_totals, [ITEM_SCAN_LIMIT, ITEM_SCAN_LIMIT])

    def test_duplicate_items_consume_batch_budget_and_post_exhaustion_items_do_no_file_io(self) -> None:
        self._write_two_line_boundary("boundary.txt", ITEM_SCAN_LIMIT)
        original_open_regular = fs_read._open_regular_at
        opened: list[tuple[str, ...]] = []

        def measured_open(cwd_fd: int, components: tuple[str, ...]) -> int:
            opened.append(components)
            return original_open_regular(cwd_fd, components)

        with patch.object(fs_read, "_open_regular_at", side_effect=measured_open):
            results = self._call(*[
                FsReadItem(path="boundary.txt", start_line=2, end_line=2)
                for _ in range(5)
            ])

        self.assertEqual([item["status"] for item in results[:4]], ["ok"] * 4)
        self.assertEqual(results[4]["error_code"], "BATCH_SCAN_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[4])
        self.assertEqual(opened, [("boundary.txt",)] * 4)

    def test_simultaneous_item_and_batch_exhaustion_prefers_batch_and_stops_remaining_io(self) -> None:
        self._write_two_line_boundary("complete.txt", ITEM_SCAN_LIMIT)
        (self.cwd / "incomplete.txt").write_bytes(b"z" * ITEM_SCAN_LIMIT)
        (self.cwd / "never-open.txt").write_text("must-not-open", encoding="utf-8")
        original_open_regular = fs_read._open_regular_at
        opened: list[tuple[str, ...]] = []

        def measured_open(cwd_fd: int, components: tuple[str, ...]) -> int:
            opened.append(components)
            return original_open_regular(cwd_fd, components)

        with patch.object(fs_read, "_open_regular_at", side_effect=measured_open):
            results = self._call(
                FsReadItem(path="complete.txt", start_line=2, end_line=2),
                FsReadItem(path="complete.txt", start_line=2, end_line=2),
                FsReadItem(path="complete.txt", start_line=2, end_line=2),
                FsReadItem(path="incomplete.txt", start_line=2),
                FsReadItem(path="never-open.txt"),
            )

        self.assertEqual([item["status"] for item in results[:3]], ["ok"] * 3)
        self.assertEqual(results[3]["error_code"], "BATCH_SCAN_LIMIT_EXCEEDED")
        self.assertEqual(results[4]["error_code"], "BATCH_SCAN_LIMIT_EXCEEDED")
        self.assertTrue(all("text" not in item for item in results[3:]))
        self.assertEqual(opened, [
            ("complete.txt",),
            ("complete.txt",),
            ("complete.txt",),
            ("incomplete.txt",),
        ])


class MCPInputBoundednessTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_all_public_input_schemas_are_closed_and_publish_representable_new_bounds(self) -> None:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        self.assertEqual(set(tools), set(server.PUBLIC_TOOL_NAMES))
        for name, tool in tools.items():
            self.assertIs(tool.input_schema.get("additionalProperties"), False, name)

        exec_props = tools["terminal_exec"].input_schema["properties"]
        self.assertEqual(exec_props["argv"]["maxItems"], 128)
        self.assertEqual(exec_props["argv"]["items"]["maxLength"], ARGV_ITEM_LIMIT)

        start_props = tools["terminal_start"].input_schema["properties"]
        self.assertEqual(start_props["argv"]["maxItems"], 128)
        self.assertEqual(start_props["argv"]["items"]["maxLength"], ARGV_ITEM_LIMIT)

        poll_props = tools["terminal_poll"].input_schema["properties"]
        self.assertEqual(poll_props["session_id"]["maxLength"], 128)

        control_props = tools["terminal_control"].input_schema["properties"]
        self.assertEqual(control_props["session_id"]["maxLength"], 128)
        self.assertEqual(_string_branch(control_props["data"])["maxLength"], TERMINAL_DATA_LIMIT)

        resize_props = tools["terminal_resize"].input_schema["properties"]
        self.assertEqual(resize_props["session_id"]["maxLength"], 128)
        for field in ("rows", "cols"):
            integer = _integer_branch(resize_props[field])
            self.assertEqual(integer["minimum"], 1)
            self.assertEqual(integer["maximum"], 65535)

        item = tools["fs_read_batch"].input_schema["$defs"]["FsReadItem"]
        self.assertEqual(_integer_branch(item["properties"]["start_line"])["maximum"], MAX_LINE)
        self.assertEqual(_integer_branch(item["properties"]["end_line"])["maximum"], MAX_LINE)

    async def test_unknown_top_level_arguments_are_rejected_before_each_delegate(self) -> None:
        session_result = {
            "session_id": "fake-session",
            "status": "running",
            "output": "",
            "next_cursor": 0,
            "cursor_expired": False,
            "dropped_output_bytes": 0,
        }
        cases = (
            (
                "terminal_exec",
                "execute_terminal",
                {"argv": ["/usr/bin/true"], "cwd": str(ROOT)},
                {
                    "cwd": str(ROOT), "argv": ["/usr/bin/true"], "exit_code": 0,
                    "timed_out": False, "stdout": "", "stderr": "",
                    "stdout_truncated": False, "stderr_truncated": False,
                },
            ),
            ("terminal_start", "_start_terminal", {"argv": ["/usr/bin/true"], "cwd": str(ROOT)}, session_result),
            ("terminal_poll", "_poll_terminal", {"session_id": "session", "cursor": 0, "wait_ms": 0}, session_result),
            ("terminal_control", "_control_terminal", {"session_id": "session", "action": "terminate"}, {"session_id": "session", "status": "exited", "exit_code": 0}),
            ("terminal_resize", "_control_terminal", {"session_id": "session", "rows": 24, "cols": 80}, {"session_id": "session", "status": "running"}),
            ("capacity_observer", "observe_capacity", {}, {"schema_version": 1, "capacity_parallelism_ceiling": 1, "reason_codes": ["unavailable"], "signals": {"probe_status": "unavailable", "sampled_window_ms": 0}}),
            ("fs_read_batch", "read_files_batch", {"cwd": str(ROOT), "items": [{"path": "README.md"}]}, {"items": []}),
        )
        async with Client(server.mcp) as client:
            for tool_name, delegate_name, arguments, delegate_result in cases:
                with self.subTest(tool=tool_name):
                    with patch.object(server, delegate_name, return_value=delegate_result) as delegate:
                        result = await client.call_tool(tool_name, {**arguments, "unknown_task0041": True})
                        self.assertTrue(result.is_error, (tool_name, result))
                        delegate.assert_not_called()

    async def test_argv_count_per_item_and_aggregate_utf8_byte_bounds(self) -> None:
        valid_result = {
            "cwd": str(ROOT),
            "argv": ["ok"],
            "exit_code": 0,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        accepted = (
            ["x"] * 127,
            ["x"] * 128,
            ["a" * (ARGV_ITEM_LIMIT - 1)],
            ["a" * ARGV_ITEM_LIMIT],
            ["é" * (ARGV_ITEM_LIMIT // 2)],
            ["a" * ARGV_ITEM_LIMIT] * 15 + ["a" * (ARGV_ITEM_LIMIT - 1)],
            ["a" * ARGV_ITEM_LIMIT] * 15 + ["a" * ARGV_ITEM_LIMIT],
        )
        rejected = (
            ["x"] * 129,
            ["a" * (ARGV_ITEM_LIMIT + 1)],
            ["é" * (ARGV_ITEM_LIMIT // 2 + 1)],
            ["a" * ARGV_ITEM_LIMIT] * 16 + ["x"],
        )

        async with Client(server.mcp) as client:
            for argv in accepted:
                with self.subTest(kind="accepted", count=len(argv), bytes=sum(len(v.encode()) for v in argv)):
                    with patch.object(server, "execute_terminal", return_value=valid_result) as delegate:
                        result = await client.call_tool("terminal_exec", {"argv": argv, "cwd": str(ROOT)})
                        self.assertFalse(result.is_error, result)
                        delegate.assert_called_once()
            for argv in rejected:
                with self.subTest(kind="rejected", count=len(argv), bytes=sum(len(v.encode()) for v in argv)):
                    with patch.object(server, "execute_terminal", return_value=valid_result) as delegate:
                        result = await client.call_tool("terminal_exec", {"argv": argv, "cwd": str(ROOT)})
                        self.assertTrue(result.is_error, result)
                        delegate.assert_not_called()

    async def test_validation_failures_use_bounded_deliberate_error_results(self) -> None:
        cases = (
            (
                "terminal_exec",
                {"argv": ["x" * (ARGV_ITEM_LIMIT + 1)], "cwd": str(ROOT)},
            ),
            (
                "terminal_control",
                {"session_id": "missing", "action": "write", "data": "x" * (TERMINAL_DATA_LIMIT + 1)},
            ),
            (
                "terminal_exec",
                {"argv": ["/usr/bin/true"], "cwd": str(ROOT), "unknown_task0041": "x" * 100_000},
            ),
        )
        async with Client(server.mcp) as client:
            for tool_name, arguments in cases:
                with self.subTest(tool=tool_name):
                    result = await client.call_tool(tool_name, arguments)
                    self.assertTrue(result.is_error, result)
                    text = _result_text(result)
                    self.assertLessEqual(len(text.encode("utf-8")), 512)
                    self.assertNotIn("x" * 1024, text)
                    self.assertNotIn("Traceback", text)

    async def test_terminal_write_session_id_and_fs_line_boundaries(self) -> None:
        control_result = {"session_id": "session", "status": "running"}
        poll_result = {
            "session_id": "session",
            "status": "running",
            "output": "",
            "next_cursor": 0,
            "cursor_expired": False,
            "dropped_output_bytes": 0,
        }
        batch_result = {"items": []}

        async with Client(server.mcp) as client:
            for data in (
                "a" * (TERMINAL_DATA_LIMIT - 1),
                "a" * TERMINAL_DATA_LIMIT,
                "é" * (TERMINAL_DATA_LIMIT // 2),
            ):
                with patch.object(server, "_control_terminal", return_value=control_result) as delegate:
                    result = await client.call_tool(
                        "terminal_control",
                        {"session_id": "session", "action": "write", "data": data},
                    )
                    self.assertFalse(result.is_error, result)
                    delegate.assert_called_once()
            for data in (
                "a" * (TERMINAL_DATA_LIMIT + 1),
                "é" * (TERMINAL_DATA_LIMIT // 2 + 1),
            ):
                with patch.object(server, "_control_terminal", return_value=control_result) as delegate:
                    result = await client.call_tool(
                        "terminal_control",
                        {"session_id": "session", "action": "write", "data": data},
                    )
                    self.assertTrue(result.is_error, result)
                    delegate.assert_not_called()

            for length in (127, 128):
                with patch.object(server, "_poll_terminal", return_value=poll_result) as delegate:
                    result = await client.call_tool(
                        "terminal_poll",
                        {"session_id": "é" * length, "cursor": 0, "wait_ms": 0},
                    )
                    self.assertFalse(result.is_error, result)
                    delegate.assert_called_once()
            with patch.object(server, "_poll_terminal", return_value=poll_result) as delegate:
                result = await client.call_tool(
                    "terminal_poll",
                    {"session_id": "s" * 129, "cursor": 0, "wait_ms": 0},
                )
                self.assertTrue(result.is_error, result)
                delegate.assert_not_called()

            for line in (MAX_LINE - 1, MAX_LINE):
                with patch.object(server, "read_files_batch", return_value=batch_result) as delegate:
                    result = await client.call_tool(
                        "fs_read_batch",
                        {"cwd": str(ROOT), "items": [{"path": "README.md", "start_line": line}]},
                    )
                    self.assertFalse(result.is_error, result)
                    delegate.assert_called_once()
            with patch.object(server, "read_files_batch", return_value=batch_result) as delegate:
                result = await client.call_tool(
                    "fs_read_batch",
                    {"cwd": str(ROOT), "items": [{"path": "README.md", "start_line": MAX_LINE + 1}]},
                )
                self.assertTrue(result.is_error, result)
                delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
