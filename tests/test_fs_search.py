from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import fs_search as fs_search_module
from agent_runtime.contracts import CapabilityFailure
from agent_runtime.fs_search import (
    CALL_DEADLINE_SECONDS,
    FS_SEARCH_CONTRACT,
    LINE_TEXT_MAX_BYTES,
    MAX_CONTENT_BYTES_SCANNED,
    MAX_FILES_SCANNED,
    MAX_RESULTS,
    MAX_SERIALIZED_RESULT_BYTES,
    search_files,
)
from agent_runtime.tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
)


class FsSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_contract_exact_authority_annotations_and_hard_bounds(self) -> None:
        self.assertEqual(FS_SEARCH_CONTRACT.name, "fs_search")
        self.assertIs(FS_SEARCH_CONTRACT.tool_class, ToolClass.READ)
        self.assertEqual(
            FS_SEARCH_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.NONE),
        )
        self.assertEqual(
            FS_SEARCH_CONTRACT.annotations,
            ToolAnnotations(True, False, True, False),
        )
        self.assertEqual(MAX_FILES_SCANNED, 20000)
        self.assertEqual(MAX_CONTENT_BYTES_SCANNED, 64 * 1024 * 1024)
        self.assertEqual(MAX_RESULTS, 500)
        self.assertEqual(MAX_SERIALIZED_RESULT_BYTES, 256 * 1024)
        self.assertEqual(CALL_DEADLINE_SECONDS, 5.0)
        self.assertEqual(LINE_TEXT_MAX_BYTES, 4096)
        self.assertEqual(
            FS_SEARCH_CONTRACT.preconditions["sensitive_dotenv_content"],
            "denied-in-content-mode",
        )
        self.assertIs(FS_SEARCH_CONTRACT.preconditions["path_mode_dotenv_discovery"], True)

    def test_path_mode_is_literal_recursive_and_deterministic(self) -> None:
        (self.root / "a.txt").write_text("x")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "b.txt").write_text("x")
        (self.root / "z.txt").write_text("x")
        result = search_files(str(self.root), ".txt", "path")
        self.assertEqual([item.path for item in result.results], ["a.txt", "dir/b.txt", "z.txt"])
        self.assertEqual([item.line_number for item in result.results], [None, None, None])
        self.assertTrue(all(item.file_sha256 is None for item in result.results))
        self.assertEqual(result.files_scanned, 3)

    def test_path_mode_casefold_is_unicode_aware(self) -> None:
        (self.root / "Straße.TXT").write_text("x")
        result = search_files(str(self.root), "STRASSE.txt", "path", case_sensitive=False)
        self.assertEqual([item.path for item in result.results], ["Straße.TXT"])

    def test_sensitive_dotenv_content_is_skipped_before_opening_and_not_counted(self) -> None:
        (self.root / "nested").mkdir()
        sensitive_paths = (".env", ".env.local", "nested/.ENV", "nested/.env.LOCAL")
        public_paths = (".ENV.EXAMPLE", ".env.sample", ".env.template", ".envrc", "notes.txt")
        payload = "synthetic-dotenv-marker\n"
        for path in sensitive_paths + public_paths:
            (self.root / path).write_text(payload, encoding="utf-8")

        original_open = fs_search_module.open_regular_at
        with patch("agent_runtime.fs_search.open_regular_at", wraps=original_open) as opened:
            result = search_files(str(self.root), "synthetic-dotenv-marker", "content")

        returned_paths = [item.path for item in result.results]
        self.assertEqual(set(returned_paths), set(public_paths))
        self.assertTrue(all(item.line_text == "synthetic-dotenv-marker" for item in result.results))
        self.assertTrue(all(item.file_sha256 is not None for item in result.results))
        self.assertEqual(result.bytes_scanned, len(payload.encode("utf-8")) * len(public_paths))
        opened_paths = ["/".join(call.args[1]) for call in opened.call_args_list]
        self.assertEqual(set(opened_paths), set(public_paths))
        self.assertTrue(set(sensitive_paths).isdisjoint(opened_paths))

    def test_path_mode_can_discover_sensitive_dotenv_paths(self) -> None:
        (self.root / "nested").mkdir()
        paths = (
            ".env",
            ".env.local",
            ".ENV.EXAMPLE",
            ".env.sample",
            ".env.template",
            ".envrc",
            "nested/.ENV",
            "nested/.env.LOCAL",
        )
        for path in paths:
            (self.root / path).write_text("synthetic\n", encoding="utf-8")

        result = search_files(str(self.root), ".env", "path", case_sensitive=False)

        self.assertEqual(set(item.path for item in result.results), set(paths))
        self.assertTrue(all(item.line_number is None for item in result.results))
        self.assertTrue(all(item.line_text is None for item in result.results))
        self.assertTrue(all(item.file_sha256 is None for item in result.results))
        self.assertEqual(result.bytes_scanned, 0)

    def test_content_mode_returns_matching_lines_and_complete_raw_sha256(self) -> None:
        raw = "alpha\nneedle here\nneedle again\n".encode()
        (self.root / "a.txt").write_bytes(raw)
        result = search_files(str(self.root), "needle", "content")
        self.assertEqual([item.line_number for item in result.results], [2, 3])
        self.assertEqual([item.line_text for item in result.results], ["needle here", "needle again"])
        expected = hashlib.sha256(raw).hexdigest()
        self.assertEqual({item.file_sha256 for item in result.results}, {expected})
        self.assertEqual(result.bytes_scanned, len(raw))

    def test_content_mode_casefold(self) -> None:
        (self.root / "a.txt").write_text("Straße\n", encoding="utf-8")
        result = search_files(str(self.root), "STRASSE", "content", case_sensitive=False)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].line_number, 1)

    def test_git_directories_symlinks_invalid_utf8_and_nul_are_skipped(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".git" / "hidden.txt").write_text("needle")
        (self.root / "good.txt").write_text("needle")
        (self.root / "nul.bin").write_bytes(b"needle\x00data")
        (self.root / "bad.bin").write_bytes(b"needle\xff")
        (self.root / "target.txt").write_text("needle")
        (self.root / "link.txt").symlink_to(self.root / "target.txt")
        result = search_files(str(self.root), "needle", "content")
        paths = [item.path for item in result.results]
        self.assertIn("good.txt", paths)
        self.assertIn("target.txt", paths)
        self.assertNotIn(".git/hidden.txt", paths)
        self.assertNotIn("link.txt", paths)
        self.assertEqual(result.skipped_nul, 1)
        self.assertEqual(result.skipped_invalid_utf8, 1)
        self.assertEqual(result.skipped_symlinks, 1)

    def test_max_results_stops_with_truthful_partial_success(self) -> None:
        for index in range(3):
            (self.root / f"{index}.txt").write_text("needle\n")
        result = search_files(str(self.root), "needle", "content", max_results=1)
        self.assertEqual(len(result.results), 1)
        self.assertTrue(result.truncated)
        self.assertEqual(result.limit_reason, "max_results")

    def test_file_scan_bound_stops_before_scanning_an_extra_file(self) -> None:
        (self.root / "a.txt").write_text("x")
        (self.root / "b.txt").write_text("x")
        with patch("agent_runtime.fs_search.MAX_FILES_SCANNED", 1):
            result = search_files(str(self.root), "no-match", "path")
        self.assertEqual(result.files_scanned, 1)
        self.assertTrue(result.truncated)
        self.assertEqual(result.limit_reason, "max_files")

    def test_content_byte_bound_stops_without_hashing_partial_file(self) -> None:
        (self.root / "a.txt").write_text("needle-more-than-bound")
        with patch("agent_runtime.fs_search.MAX_CONTENT_BYTES_SCANNED", 4):
            result = search_files(str(self.root), "needle", "content")
        self.assertEqual(result.results, [])
        self.assertTrue(result.truncated)
        self.assertEqual(result.limit_reason, "max_bytes")
        self.assertEqual(result.bytes_scanned, 0)

    def test_serialized_output_bound_is_enforced(self) -> None:
        (self.root / "a.txt").write_text("needle\n")
        with patch("agent_runtime.fs_search.MAX_SERIALIZED_RESULT_BYTES", 1024):
            result = search_files(str(self.root), "needle", "content")
        self.assertTrue(result.truncated)
        self.assertEqual(result.limit_reason, "max_output")
        self.assertEqual(result.results, [])

    def test_deadline_produces_partial_success_not_an_unbounded_scan(self) -> None:
        (self.root / "a.txt").write_text("needle")
        with patch("agent_runtime.fs_search.time.monotonic", side_effect=[0.0, 6.0]):
            result = search_files(str(self.root), "needle", "path")
        self.assertTrue(result.truncated)
        self.assertEqual(result.limit_reason, "deadline")
        self.assertEqual(result.files_scanned, 0)

    def test_deadline_is_enforced_while_reading_content(self) -> None:
        (self.root / "a.txt").write_text("needle")
        with patch(
            "agent_runtime.fs_search.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0, 6.0],
        ):
            result = search_files(str(self.root), "needle", "content")
        self.assertTrue(result.truncated)
        self.assertEqual(result.limit_reason, "deadline")
        self.assertEqual(result.results, [])

    def test_line_text_truncation_preserves_utf8_boundary(self) -> None:
        line = "needle-" + ("é" * 3000)
        (self.root / "a.txt").write_text(line + "\n", encoding="utf-8")
        result = search_files(str(self.root), "needle", "content")
        self.assertEqual(len(result.results), 1)
        item = result.results[0]
        self.assertTrue(item.line_truncated)
        self.assertLessEqual(len(item.line_text.encode("utf-8")), 4096)
        item.line_text.encode("utf-8", errors="strict")

    def test_root_symlink_is_rejected(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (self.root / "link").symlink_to(target, target_is_directory=True)
        with self.assertRaises(CapabilityFailure) as caught:
            search_files(str(self.root), "x", "path", root_path="link")
        self.assertEqual(caught.exception.reason_code, "SYMLINK_DISALLOWED")

    def test_invalid_query_mode_and_root_path_are_rejected(self) -> None:
        cases = [
            ("", "path", "."),
            ("x", "regex", "."),
            ("x", "path", "../outside"),
        ]
        for query, mode, root in cases:
            with self.subTest(query=query, mode=mode, root=root):
                with self.assertRaises(CapabilityFailure) as caught:
                    search_files(str(self.root), query, mode, root_path=root)
                self.assertEqual(caught.exception.code, ContractErrorCode.INVALID_ARGUMENT)


if __name__ == "__main__":
    unittest.main()
