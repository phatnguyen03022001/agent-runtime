from __future__ import annotations

import hashlib
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client

from agent_runtime import fs_read, server
from agent_runtime.contracts import FsReadItem
from agent_runtime.errors import RuntimeValidationError
from agent_runtime.fs_read import read_files_batch

ITEM_LIMIT = 128 * 1024
BATCH_LIMIT = 256 * 1024


class FsReadBatchAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-runtime-task0040-adversarial-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _call(self, *items: FsReadItem) -> list[dict[str, object]]:
        return read_files_batch(str(self.cwd), list(items))["items"]  # type: ignore[index,return-value]

    def test_selected_range_utf8_ignores_unselected_invalid_bytes_across_chunk_layouts(self) -> None:
        path = self.cwd / "range.txt"
        path.write_bytes(b"ok\n\xffsuffix")

        with patch.object(fs_read, "_READ_CHUNK_BYTES", 64 * 1024):
            same_chunk = self._call(FsReadItem(path="range.txt", start_line=1, end_line=1))[0]
        with patch.object(fs_read, "_READ_CHUNK_BYTES", 3):
            later_chunk = self._call(FsReadItem(path="range.txt", start_line=1, end_line=1))[0]

        expected = {
            "status": "ok",
            "path": "range.txt",
            "start_line": 1,
            "end_line": 1,
            "text": "ok\n",
        }
        expected_sha = hashlib.sha256(b"ok\n\xffsuffix").hexdigest()
        for result in (same_chunk, later_chunk):
            self.assertEqual({key: result[key] for key in expected}, expected)
            self.assertEqual(result["size_bytes"], 10)
            self.assertEqual(result["returned_bytes"], 3)
            self.assertTrue(result["eof"])
            self.assertFalse(result["truncated"])
            self.assertEqual(result["sha256"], expected_sha)

        path.write_bytes(b"\xffprefix\nok\n")
        prefix_unselected = self._call(FsReadItem(path="range.txt", start_line=2, end_line=2))[0]
        self.assertEqual(prefix_unselected["status"], "ok")
        self.assertEqual(prefix_unselected["text"], "ok\n")

        path.write_bytes(b"ok\n\xffselected")
        selected_invalid = self._call(FsReadItem(path="range.txt", start_line=2, end_line=2))[0]
        self.assertEqual(selected_invalid["status"], "error")
        self.assertEqual(selected_invalid["error_code"], "INVALID_UTF8")
        self.assertNotIn("text", selected_invalid)

    def test_cwd_intermediate_component_substitution_cannot_redirect_anchor(self) -> None:
        trusted = self.root / "trusted"
        trusted.mkdir()
        cwd = trusted / "cwd"
        cwd.mkdir()
        (cwd / "victim.txt").write_text("safe", encoding="utf-8")
        moved = self.root / "trusted-original"

        with tempfile.TemporaryDirectory(prefix="agent-runtime-task0040-cwd-outside-") as outside_dir:
            outside = Path(outside_dir)
            redirected_cwd = outside / "cwd"
            redirected_cwd.mkdir()
            (redirected_cwd / "victim.txt").write_text("CWD_RACE_SECRET", encoding="utf-8")
            expected_cwd = str(cwd.resolve())
            original_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == expected_cwd and dir_fd is None and not swapped:
                    swapped = True
                    trusted.rename(moved)
                    trusted.symlink_to(outside, target_is_directory=True)
                if dir_fd is None:
                    return original_open(path, flags, mode)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with patch("agent_runtime.fs_read.os.open", side_effect=racing_open):
                with self.assertRaisesRegex(RuntimeValidationError, "cwd could not be opened safely"):
                    read_files_batch(str(cwd), [FsReadItem(path="victim.txt")])

        self.assertTrue(swapped)

    def test_cwd_intermediate_directory_replacement_cannot_redirect_anchor(self) -> None:
        trusted = self.root / "trusted-directory"
        trusted.mkdir()
        cwd = trusted / "cwd"
        cwd.mkdir()
        (cwd / "victim.txt").write_text("safe", encoding="utf-8")
        moved = self.root / "trusted-directory-original"
        replacement = self.root / "replacement-directory"
        replacement.mkdir()
        replacement_cwd = replacement / "cwd"
        replacement_cwd.mkdir()
        (replacement_cwd / "victim.txt").write_text("RENAMED_SECRET", encoding="utf-8")
        expected_cwd = str(cwd.resolve())
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == expected_cwd and dir_fd is None and not swapped:
                swapped = True
                trusted.rename(moved)
                replacement.rename(trusted)
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with patch("agent_runtime.fs_read.os.open", side_effect=racing_open):
            with self.assertRaisesRegex(RuntimeValidationError, "cwd could not be opened safely"):
                read_files_batch(str(cwd), [FsReadItem(path="victim.txt")])

        self.assertTrue(swapped)

    def test_intermediate_component_symlink_substitution_race_cannot_escape(self) -> None:
        safe_dir = self.cwd / "safe"
        safe_dir.mkdir()
        (safe_dir / "victim.txt").write_text("safe", encoding="utf-8")
        moved_dir = self.cwd / "safe-original"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("INTERMEDIATE_RACE_SECRET", encoding="utf-8")
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "safe" and dir_fd is not None and not swapped:
                swapped = True
                safe_dir.rename(moved_dir)
                safe_dir.symlink_to(outside, target_is_directory=True)
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)
        with patch("agent_runtime.fs_read.os.open", side_effect=racing_open):
            result = self._call(FsReadItem(path="safe/victim.txt"))[0]

        self.assertTrue(swapped)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "SYMLINK_DISALLOWED")
        self.assertNotIn("INTERMEDIATE_RACE_SECRET", repr(result))

    def test_fifo_socket_and_device_modes_are_rejected_before_content_read(self) -> None:
        fifo = self.cwd / "pipe"
        os.mkfifo(fifo)
        sock_path = self.cwd / "socket"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        self.addCleanup(sock.close)

        with patch("agent_runtime.fs_read.os.read", side_effect=AssertionError("special file read")):
            results = self._call(FsReadItem(path="pipe"), FsReadItem(path="socket"))
        self.assertEqual([item["error_code"] for item in results], ["NOT_REGULAR_FILE", "NOT_REGULAR_FILE"])

        original_nofollow = fs_read._nofollow_stat
        original_open = os.open
        for mode in (stat.S_IFCHR | 0o600, stat.S_IFBLK | 0o600):
            with self.subTest(mode=mode):
                def fake_nofollow(parent_fd: int, component: str):
                    if component == "device":
                        return SimpleNamespace(st_mode=mode)
                    return original_nofollow(parent_fd, component)

                def guarded_open(path, flags, file_mode=0o777, *, dir_fd=None):
                    if path == "device":
                        raise AssertionError("special device must not be opened")
                    if dir_fd is None:
                        return original_open(path, flags, file_mode)
                    return original_open(path, flags, file_mode, dir_fd=dir_fd)

                with (
                    patch("agent_runtime.fs_read._nofollow_stat", side_effect=fake_nofollow),
                    patch("agent_runtime.fs_read.os.open", side_effect=guarded_open),
                    patch("agent_runtime.fs_read.os.read", side_effect=AssertionError("device read")),
                ):
                    result = self._call(FsReadItem(path="device"))[0]
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error_code"], "NOT_REGULAR_FILE")
                self.assertNotIn("text", result)

    def test_item_and_batch_byte_boundaries_below_at_and_above(self) -> None:
        (self.cwd / "at.txt").write_bytes(b"a" * ITEM_LIMIT)
        (self.cwd / "below.txt").write_bytes(b"b" * (ITEM_LIMIT - 1))
        (self.cwd / "one.txt").write_bytes(b"c")
        (self.cwd / "over.txt").write_bytes(b"d" * (ITEM_LIMIT + 1))

        results = self._call(
            FsReadItem(path="at.txt"),
            FsReadItem(path="below.txt"),
            FsReadItem(path="one.txt"),
            FsReadItem(path="one.txt"),
            FsReadItem(path="over.txt"),
        )

        self.assertEqual(len(results[0]["text"].encode("utf-8")), ITEM_LIMIT)
        self.assertEqual(len(results[1]["text"].encode("utf-8")), ITEM_LIMIT - 1)
        self.assertEqual(results[2]["status"], "ok")

        self.assertEqual(
            sum(len(item.get("text", "").encode("utf-8")) for item in results[:3]),
            BATCH_LIMIT,
        )
        self.assertEqual(results[3]["error_code"], "BATCH_OUTPUT_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[3])
        self.assertEqual(results[4]["error_code"], "ITEM_OUTPUT_LIMIT_EXCEEDED")
        self.assertNotIn("text", results[4])


class FsReadBatchCardinalityClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_accepts_one_and_twenty_items(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workspace_root = root.parent
        env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace_root)})
        env.start()
        self.addCleanup(env.stop)

        async with Client(server.mcp) as client:
            one = await client.call_tool(
                "fs_read_batch",
                {"cwd": str(root), "items": [{"path": "README.md", "start_line": 1, "end_line": 1}]},
            )
            twenty = await client.call_tool(
                "fs_read_batch",
                {"cwd": str(root), "items": [{"path": "README.md", "start_line": 1, "end_line": 1}] * 20},
            )

        self.assertFalse(one.is_error)
        self.assertFalse(twenty.is_error)
        self.assertEqual(len(one.structured_content["items"]), 1)
        self.assertEqual(len(twenty.structured_content["items"]), 20)
        self.assertTrue(all(item["status"] == "ok" for item in twenty.structured_content["items"]))


if __name__ == "__main__":
    unittest.main()
