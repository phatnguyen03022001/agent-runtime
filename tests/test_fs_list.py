from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.contracts import CapabilityFailure
from agent_runtime.fs_list import (
    DEFAULT_MAX_ENTRIES,
    DIRECTORY_SCAN_LIMIT,
    FS_LIST_CONTRACT,
    MAX_ENTRIES,
    list_directory,
)
from agent_runtime.tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
)


class FsListTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_contract_exact_authority_annotations_and_bounds(self) -> None:
        self.assertEqual(FS_LIST_CONTRACT.name, "fs_list")
        self.assertIs(FS_LIST_CONTRACT.tool_class, ToolClass.READ)
        self.assertEqual(
            FS_LIST_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.NONE),
        )
        self.assertEqual(
            FS_LIST_CONTRACT.annotations,
            ToolAnnotations(True, False, True, False),
        )
        self.assertEqual(DEFAULT_MAX_ENTRIES, 200)
        self.assertEqual(MAX_ENTRIES, 1000)
        self.assertEqual(DIRECTORY_SCAN_LIMIT, 10000)
        self.assertEqual(FS_LIST_CONTRACT.bounds["max_entries"], 1000)
        self.assertEqual(FS_LIST_CONTRACT.bounds["directory_scan_entries"], 10000)

    def test_non_recursive_deterministic_sort_and_classification(self) -> None:
        (self.root / "z.txt").write_text("zzz", encoding="utf-8")
        (self.root / "a-dir").mkdir()
        (self.root / "a-dir" / "nested.txt").write_text("hidden", encoding="utf-8")
        (self.root / "m-link").symlink_to(self.root / "z.txt")
        fifo = self.root / "n-fifo"
        os.mkfifo(fifo)

        result = list_directory(str(self.root), ".")
        self.assertEqual([entry.name for entry in result.entries], ["a-dir", "m-link", "n-fifo", "z.txt"])
        by_name = {entry.name: entry for entry in result.entries}
        self.assertEqual(by_name["a-dir"].kind, "directory")
        self.assertEqual(by_name["m-link"].kind, "symlink")
        self.assertEqual(by_name["n-fifo"].kind, "other")
        self.assertEqual(by_name["z.txt"].kind, "file")
        self.assertEqual(by_name["z.txt"].size_bytes, 3)
        self.assertNotIn("nested.txt", by_name)
        self.assertEqual(result.path, ".")
        self.assertEqual(result.scanned_entries, 4)
        self.assertFalse(result.truncated)

    def test_descendant_directory_path_is_normalized_and_non_recursive(self) -> None:
        child = self.root / "dir"
        child.mkdir()
        (child / "b").write_text("b")
        (child / "a").write_text("a")
        result = list_directory(str(self.root), "dir")
        self.assertEqual(result.path, "dir")
        self.assertEqual([entry.path for entry in result.entries], ["dir/a", "dir/b"])

    def test_max_entries_returns_first_sorted_entries_and_truncates(self) -> None:
        for name in ("c", "a", "b"):
            (self.root / name).write_text(name)
        result = list_directory(str(self.root), ".", max_entries=2)
        self.assertEqual([entry.name for entry in result.entries], ["a", "b"])
        self.assertTrue(result.truncated)
        self.assertEqual(result.scanned_entries, 3)

    def test_symlink_directory_path_is_never_traversed(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (self.root / "link").symlink_to(target, target_is_directory=True)
        with self.assertRaises(CapabilityFailure) as caught:
            list_directory(str(self.root), "link")
        self.assertEqual(caught.exception.code, ContractErrorCode.PRECONDITION_FAILED)
        self.assertEqual(caught.exception.reason_code, "SYMLINK_DISALLOWED")

    def test_invalid_descendant_paths_are_rejected(self) -> None:
        for value in ("/tmp", "../x", "x/../y", "./x", "x//y", ""):
            with self.subTest(path=value):
                with self.assertRaises(CapabilityFailure) as caught:
                    list_directory(str(self.root), value)
                self.assertEqual(caught.exception.code, ContractErrorCode.INVALID_ARGUMENT)

    def test_scan_ceiling_fails_instead_of_returning_partial_listing(self) -> None:
        for index in range(4):
            (self.root / f"f{index}").write_text("x")
        with patch("agent_runtime.fs_list.DIRECTORY_SCAN_LIMIT", 3):
            with self.assertRaises(CapabilityFailure) as caught:
                list_directory(str(self.root), ".")
        self.assertEqual(caught.exception.code, ContractErrorCode.LIMIT_EXCEEDED)
        self.assertEqual(caught.exception.reason_code, "DIRECTORY_SCAN_LIMIT")

    def test_invalid_utf8_names_are_skipped_and_counted(self) -> None:
        directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                fd = os.open(b"\xff", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=directory_fd)
            except (OSError, TypeError):
                self.skipTest("filesystem does not permit invalid UTF-8 byte names")
            else:
                os.close(fd)
        finally:
            os.close(directory_fd)
        result = list_directory(str(self.root), ".")
        self.assertEqual(result.entries, [])
        self.assertEqual(result.scanned_entries, 1)
        self.assertEqual(result.skipped_invalid_names, 1)

    def test_outside_workspace_cwd_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(CapabilityFailure) as caught:
                list_directory(outside, ".")
        self.assertEqual(caught.exception.code, ContractErrorCode.OUTSIDE_WORKSPACE)


if __name__ == "__main__":
    unittest.main()
