from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import server
from agent_runtime.contracts import FsReadItem
from agent_runtime.errors import RuntimeValidationError
from agent_runtime.fs_read import read_files_batch

ITEM_LIMIT = 128 * 1024
BATCH_LIMIT = 256 * 1024


class FsReadBatchContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_surface_includes_exact_fs_read_batch_annotations(self) -> None:
        tools = await server.mcp.list_tools()
        self.assertEqual(
            tuple(tool.name for tool in tools),
            (
                "terminal_exec",
                "terminal_start",
                "terminal_poll",
                "terminal_control",
                "terminal_resize",
                "capacity_observer",
                "fs_read_batch",
                "repo_observer",
            ),
        )
        tool = next(tool for tool in tools if tool.name == "fs_read_batch")
        annotations = tool.annotations.model_dump(by_alias=True)
        self.assertEqual(
            (
                annotations["readOnlyHint"],
                annotations["destructiveHint"],
                annotations["idempotentHint"],
                annotations["openWorldHint"],
            ),
            (True, False, True, False),
        )


class FsReadBatchBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-runtime-task0040-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _call(self, *items: FsReadItem) -> list[dict[str, object]]:
        return read_files_batch(str(self.cwd), list(items))["items"]  # type: ignore[index,return-value]

    def test_ordered_full_range_duplicate_and_eof_semantics(self) -> None:
        (self.cwd / "a.txt").write_bytes(b"one\r\ntwo\nthree\r")
        (self.cwd / "b.txt").write_text("alpha\nbeta", encoding="utf-8")

        results = self._call(
            FsReadItem(path="a.txt"),
            FsReadItem(path="a.txt", start_line=2, end_line=2),
            FsReadItem(path="b.txt", start_line=1, end_line=99),
            FsReadItem(path="a.txt", start_line=20),
            FsReadItem(path="a.txt", start_line=2, end_line=2),
        )

        self.assertEqual(
            results,
            [
                {"status": "ok", "path": "a.txt", "start_line": 1, "end_line": None, "text": "one\r\ntwo\nthree\r"},
                {"status": "ok", "path": "a.txt", "start_line": 2, "end_line": 2, "text": "two\n"},
                {"status": "ok", "path": "b.txt", "start_line": 1, "end_line": 99, "text": "alpha\nbeta"},
                {"status": "ok", "path": "a.txt", "start_line": 20, "end_line": None, "text": ""},
                {"status": "ok", "path": "a.txt", "start_line": 2, "end_line": 2, "text": "two\n"},
            ],
        )

    def test_lexical_path_and_range_relationship_are_request_failures(self) -> None:
        invalid_paths = (
            "/absolute.txt",
            "../escape.txt",
            "a/../escape.txt",
            "a/./file.txt",
            "a//file.txt",
            ".",
            "a/\x00file.txt",
        )
        for path in invalid_paths:
            with self.subTest(path=repr(path)):
                with self.assertRaises(RuntimeValidationError):
                    self._call(FsReadItem(path=path))
        with self.assertRaises(RuntimeValidationError):
            self._call(FsReadItem(path="a.txt", start_line=3, end_line=2))

    def test_expected_item_failures_are_partial_bounded_and_sanitized(self) -> None:
        (self.cwd / "good.txt").write_text("good\n", encoding="utf-8")
        (self.cwd / "bad-utf8.txt").write_bytes(b"ok\n\xffbad")
        (self.cwd / "directory").mkdir()
        denied = self.cwd / "denied.txt"
        denied.write_text("denied", encoding="utf-8")
        denied.chmod(0)
        self.addCleanup(lambda: denied.chmod(0o600) if denied.exists() else None)
        (self.cwd / "target.txt").write_text("target", encoding="utf-8")
        (self.cwd / "final-link").symlink_to("target.txt")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SHOULD_NOT_LEAK", encoding="utf-8")
        (self.cwd / "mid-link").symlink_to(outside, target_is_directory=True)

        results = self._call(
            FsReadItem(path="good.txt"),
            FsReadItem(path="missing.txt"),
            FsReadItem(path="bad-utf8.txt"),
            FsReadItem(path="directory"),
            FsReadItem(path="denied.txt"),
            FsReadItem(path="final-link"),
            FsReadItem(path="mid-link/secret.txt"),
        )

        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(
            [item.get("error_code") for item in results[1:]],
            [
                "NOT_FOUND",
                "INVALID_UTF8",
                "NOT_REGULAR_FILE",
                "ACCESS_DENIED",
                "SYMLINK_DISALLOWED",
                "SYMLINK_DISALLOWED",
            ],
        )
        serialized = repr(results)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("SHOULD_NOT_LEAK", serialized)
        for item in results[1:]:
            self.assertNotIn("text", item)
            self.assertLessEqual(len(str(item["message"])), 160)

    def test_fifo_and_unix_socket_are_rejected_as_non_regular(self) -> None:
        fifo = self.cwd / "pipe"
        os.mkfifo(fifo)
        sock_path = self.cwd / "socket"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        self.addCleanup(sock.close)

        results = self._call(FsReadItem(path="pipe"), FsReadItem(path="socket"))
        self.assertEqual([item["error_code"] for item in results], ["NOT_REGULAR_FILE", "NOT_REGULAR_FILE"])

    def test_final_component_symlink_replacement_race_cannot_escape(self) -> None:
        victim = self.cwd / "victim.txt"
        victim.write_text("safe", encoding="utf-8")
        outside = self.root / "outside-secret.txt"
        outside.write_text("RACE_SECRET_MUST_NOT_LEAK", encoding="utf-8")
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "victim.txt" and dir_fd is not None and not swapped:
                swapped = True
                victim.unlink()
                victim.symlink_to(outside)
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with patch("agent_runtime.fs_read.os.open", side_effect=racing_open):
            result = self._call(FsReadItem(path="victim.txt"))[0]

        self.assertTrue(swapped)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "SYMLINK_DISALLOWED")
        self.assertNotIn("RACE_SECRET_MUST_NOT_LEAK", repr(result))

    def test_item_limit_uses_utf8_bytes_and_never_truncates(self) -> None:
        (self.cwd / "ascii-at.txt").write_bytes(b"a" * ITEM_LIMIT)
        (self.cwd / "ascii-over.txt").write_bytes(b"a" * (ITEM_LIMIT + 1))
        (self.cwd / "multi-at.txt").write_text("é" * (ITEM_LIMIT // 2), encoding="utf-8")
        (self.cwd / "multi-over.txt").write_text("é" * (ITEM_LIMIT // 2 + 1), encoding="utf-8")

        results = self._call(
            FsReadItem(path="ascii-at.txt"),
            FsReadItem(path="ascii-over.txt"),
            FsReadItem(path="multi-at.txt"),
            FsReadItem(path="multi-over.txt"),
        )

        self.assertEqual(len(results[0]["text"].encode("utf-8")), ITEM_LIMIT)
        self.assertEqual(results[1]["error_code"], "ITEM_OUTPUT_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[1])
        self.assertEqual(len(results[2]["text"].encode("utf-8")), ITEM_LIMIT)
        self.assertEqual(results[3]["error_code"], "ITEM_OUTPUT_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[3])

    def test_aggregate_limit_rejects_complete_item_and_continues_ordered_processing(self) -> None:
        (self.cwd / "first.txt").write_bytes(b"a" * ITEM_LIMIT)
        (self.cwd / "second.txt").write_bytes(b"b" * ITEM_LIMIT)
        (self.cwd / "third.txt").write_text("x", encoding="utf-8")
        (self.cwd / "later-empty.txt").write_text("present", encoding="utf-8")

        results = self._call(
            FsReadItem(path="first.txt"),
            FsReadItem(path="second.txt"),
            FsReadItem(path="third.txt"),
            FsReadItem(path="later-empty.txt", start_line=99),
        )

        self.assertEqual(sum(len(item.get("text", "").encode("utf-8")) for item in results), BATCH_LIMIT)
        self.assertEqual(results[2]["error_code"], "BATCH_OUTPUT_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[2])
        self.assertEqual(results[3]["status"], "ok")
        self.assertEqual(results[3]["text"], "")


if __name__ == "__main__":
    unittest.main()
