from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import server, tool_contract
from agent_runtime.contracts import CapabilityFailure, ContinuationReceiptResult
from agent_runtime.fs_list import list_directory
from agent_runtime.fs_search import MAX_SERIALIZED_RESULT_BYTES, search_files
from agent_runtime.repo_diff import diff_repository
from agent_runtime.repo_observer import RepoObserverFailure, observe_repository
from agent_runtime.tool_contract import (
    CONTINUATION_CURSOR_MAX_CHARS,
    CONTINUATION_TTL_SECONDS,
    ContinuationFailure,
    ContractErrorCode,
    make_continuation_cursor,
    parse_continuation_cursor,
)


class ContinuationCodecTests(unittest.TestCase):
    def test_cursor_is_bounded_stateless_tamper_evident_and_expires(self) -> None:
        parameters = {"path": ".", "max_entries": 2}
        receipt = "a" * 64
        cursor = make_continuation_cursor(
            tool="fs_list",
            position=2,
            semantic_parameters=parameters,
            receipt_digest=receipt,
            now=1_000,
        )
        self.assertTrue(cursor.isascii())
        self.assertLessEqual(len(cursor), CONTINUATION_CURSOR_MAX_CHARS)
        self.assertEqual(
            parse_continuation_cursor(
                cursor,
                tool="fs_list",
                semantic_parameters=parameters,
                receipt_digest=receipt,
                now=1_000 + CONTINUATION_TTL_SECONDS - 1,
            ),
            2,
        )

        with self.assertRaises(ContinuationFailure) as expired:
            parse_continuation_cursor(
                cursor,
                tool="fs_list",
                semantic_parameters=parameters,
                receipt_digest=receipt,
                now=1_000 + CONTINUATION_TTL_SECONDS,
            )
        self.assertEqual(expired.exception.reason_code, "CONTINUATION_EXPIRED")

        with self.assertRaises(ContinuationFailure) as wrong_tool:
            parse_continuation_cursor(
                cursor,
                tool="fs_search",
                semantic_parameters=parameters,
                receipt_digest=receipt,
                now=1_001,
            )
        self.assertEqual(wrong_tool.exception.reason_code, "CONTINUATION_TOOL_MISMATCH")

        with self.assertRaises(ContinuationFailure) as params:
            parse_continuation_cursor(
                cursor,
                tool="fs_list",
                semantic_parameters={"path": ".", "max_entries": 3},
                receipt_digest=receipt,
                now=1_001,
            )
        self.assertEqual(params.exception.reason_code, "CONTINUATION_PARAMETER_MISMATCH")

        with self.assertRaises(ContinuationFailure) as receipt_mismatch:
            parse_continuation_cursor(
                cursor,
                tool="fs_list",
                semantic_parameters=parameters,
                receipt_digest="b" * 64,
                now=1_001,
            )
        self.assertEqual(
            receipt_mismatch.exception.reason_code,
            "CONTINUATION_RECEIPT_MISMATCH",
        )

        with self.assertRaises(ContinuationFailure) as malformed:
            parse_continuation_cursor(
                "not*base64",
                tool="fs_list",
                semantic_parameters=parameters,
                receipt_digest=receipt,
                now=1_001,
            )
        self.assertEqual(
            malformed.exception.reason_code,
            "INVALID_CONTINUATION_CURSOR",
        )

        replacement = "A" if cursor[-1] != "A" else "B"
        tampered = cursor[:-1] + replacement
        with self.assertRaises(ContinuationFailure) as integrity:
            parse_continuation_cursor(
                tampered,
                tool="fs_list",
                semantic_parameters=parameters,
                receipt_digest=receipt,
                now=1_001,
            )
        self.assertIn(
            integrity.exception.reason_code,
            {"INVALID_CONTINUATION_CURSOR", "CONTINUATION_INTEGRITY_MISMATCH"},
        )

        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw[:-32].decode("utf-8"))
        payload["v"] = 2
        payload_bytes = tool_contract.canonical_structured_bytes(payload)
        wrong_version_cursor = base64.urlsafe_b64encode(
            payload_bytes + tool_contract._continuation_integrity(payload_bytes)
        ).rstrip(b"=").decode("ascii")
        with self.assertRaises(ContinuationFailure) as version:
            parse_continuation_cursor(
                wrong_version_cursor,
                tool="fs_list",
                semantic_parameters=parameters,
                receipt_digest=receipt,
                now=1_001,
            )
        self.assertEqual(
            version.exception.reason_code,
            "CONTINUATION_VERSION_MISMATCH",
        )


class FilesystemContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_fs_list_pages_are_deterministic_complete_and_state_bound(self) -> None:
        for name in ("e.txt", "a.txt", "d.txt", "b.txt", "c.txt"):
            (self.root / name).write_text(name, encoding="utf-8")

        with patch("agent_runtime.tool_contract.time.time", return_value=10_000):
            deterministic = list_directory(str(self.root), ".", 2)
            repeated = list_directory(str(self.root), ".", 2)
        self.assertEqual(deterministic.model_dump(), repeated.model_dump())

        first = list_directory(str(self.root), ".", 2)
        self.assertEqual(first.schema_version, 2)
        self.assertEqual(first.continuation_receipt.kind, "fs-list")

        pages = [first]
        while pages[-1].next_cursor is not None:
            previous = pages[-1]
            self.assertLessEqual(
                len(previous.next_cursor),
                CONTINUATION_CURSOR_MAX_CHARS,
            )
            pages.append(
                list_directory(
                    str(self.root),
                    ".",
                    2,
                    previous.next_cursor,
                    previous.continuation_receipt,
                )
            )

        names = [entry.name for page in pages for entry in page.entries]
        self.assertEqual(names, ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"])
        self.assertEqual(len(names), len(set(names)))
        self.assertFalse(pages[-1].truncated)
        self.assertIsNone(pages[-1].next_cursor)

        with self.assertRaises(CapabilityFailure) as pair:
            list_directory(str(self.root), ".", 2, first.next_cursor, None)
        self.assertEqual(pair.exception.code, ContractErrorCode.INVALID_ARGUMENT)
        self.assertEqual(pair.exception.reason_code, "CONTINUATION_PAIR_REQUIRED")

        with self.assertRaises(CapabilityFailure) as parameter:
            list_directory(
                str(self.root),
                ".",
                3,
                first.next_cursor,
                first.continuation_receipt,
            )
        self.assertEqual(
            parameter.exception.reason_code,
            "CONTINUATION_PARAMETER_MISMATCH",
        )

        tampered_receipt = ContinuationReceiptResult(
            schema_version=1,
            kind="fs-list",
            digest="f" * 64,
        )
        with self.assertRaises(CapabilityFailure) as receipt:
            list_directory(
                str(self.root),
                ".",
                2,
                first.next_cursor,
                tampered_receipt,
            )
        self.assertEqual(
            receipt.exception.reason_code,
            "CONTINUATION_RECEIPT_MISMATCH",
        )

        (self.root / "aa-new.txt").write_text("mutation", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as changed:
            list_directory(
                str(self.root),
                ".",
                2,
                first.next_cursor,
                first.continuation_receipt,
            )
        self.assertEqual(changed.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(changed.exception.reason_code, "CONTINUATION_STATE_CHANGED")

    def test_fs_search_revalidates_prefix_and_pages_without_gaps(self) -> None:
        for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
            (self.root / name).write_text("needle\n", encoding="utf-8")

        first = search_files(str(self.root), ".txt", "path", max_results=2)
        self.assertTrue(first.truncated)
        self.assertEqual(first.limit_reason, "max_results")
        self.assertIsNotNone(first.next_cursor)

        pages = [first]
        while pages[-1].next_cursor is not None:
            previous = pages[-1]
            pages.append(
                search_files(
                    str(self.root),
                    ".txt",
                    "path",
                    max_results=2,
                    cursor=previous.next_cursor,
                    continuation_receipt=previous.continuation_receipt,
                )
            )
        paths = [item.path for page in pages for item in page.results]
        self.assertEqual(paths, ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"])
        self.assertEqual(len(paths), len(set(paths)))
        self.assertLessEqual(
            max(len(page.next_cursor or "") for page in pages),
            CONTINUATION_CURSOR_MAX_CHARS,
        )
        for page in pages:
            self.assertLessEqual(
                len(page.model_dump_json().encode("utf-8")),
                MAX_SERIALIZED_RESULT_BYTES,
            )

        original_first = first
        (self.root / "00-prefix.txt").write_text("needle\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as changed:
            search_files(
                str(self.root),
                ".txt",
                "path",
                max_results=2,
                cursor=original_first.next_cursor,
                continuation_receipt=original_first.continuation_receipt,
            )
        self.assertEqual(changed.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(changed.exception.reason_code, "CONTINUATION_STATE_CHANGED")


class GitContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Runtime Test")
        self._git("config", "user.email", "runtime@example.invalid")
        (self.repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-q", "-m", "baseline")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["/usr/bin/git", *args],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout

    def test_repo_diff_pages_preserve_utf8_and_full_raw_receipt_identity(self) -> None:
        text = "".join(f"{index:05d} é continuation payload\n" for index in range(30_000))
        (self.repo / "tracked.txt").write_text(text, encoding="utf-8")
        expected = subprocess.run(
            [
                "/usr/bin/git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=all",
                "--no-color",
                "--",
            ],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode("utf-8", errors="replace")

        pages = [diff_repository(str(self.repo), "worktree")]
        self.assertTrue(pages[0].patch_truncated)
        self.assertEqual(
            pages[0].diff_receipt.digest,
            pages[0].continuation_receipt.digest,
        )
        while pages[-1].next_cursor is not None:
            previous = pages[-1]
            pages.append(
                diff_repository(
                    str(self.repo),
                    "worktree",
                    previous.next_cursor,
                    previous.continuation_receipt,
                )
            )
        combined = "".join(page.patch for page in pages)
        combined.encode("utf-8", errors="strict")
        self.assertEqual(combined, expected)
        self.assertEqual(
            {page.diff_receipt.digest for page in pages},
            {pages[0].diff_receipt.digest},
        )
        self.assertTrue(all(page.network_used is False for page in pages))

        first = diff_repository(str(self.repo), "worktree")
        (self.repo / "tracked.txt").write_text(text + "mutation\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as changed:
            diff_repository(
                str(self.repo),
                "worktree",
                first.next_cursor,
                first.continuation_receipt,
            )
        self.assertEqual(changed.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(changed.exception.reason_code, "CONTINUATION_STATE_CHANGED")

    def test_repo_observer_pages_exact_changes_without_network_or_mutation(self) -> None:
        for name in ("a.tmp", "b.tmp", "c.tmp", "d.tmp", "e.tmp"):
            (self.repo / name).write_text(name, encoding="utf-8")

        before_head = self._git("rev-parse", "HEAD").strip()
        pages = [observe_repository(str(self.repo), 2)]
        self.assertTrue(pages[0].truncated)
        self.assertIsNotNone(pages[0].next_cursor)
        while pages[-1].next_cursor is not None:
            previous = pages[-1]
            pages.append(
                observe_repository(
                    str(self.repo),
                    2,
                    previous.next_cursor,
                    previous.continuation_receipt,
                )
            )

        paths = [change.path for page in pages for change in page.changes]
        self.assertEqual(paths, ["a.tmp", "b.tmp", "c.tmp", "d.tmp", "e.tmp"])
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(all(page.observation.fetched is False for page in pages))
        self.assertTrue(all(page.observation.network_used is False for page in pages))
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before_head)

        with patch("agent_runtime.repo_observer._STATUS_MAX_BYTES", 16):
            hard_bounded = observe_repository(str(self.repo), 2)
        self.assertTrue(hard_bounded.truncated)
        self.assertIsNone(hard_bounded.next_cursor)

        first = observe_repository(str(self.repo), 2)
        (self.repo / "aa-new.tmp").write_text("mutation", encoding="utf-8")
        with self.assertRaises(RepoObserverFailure) as changed:
            observe_repository(
                str(self.repo),
                2,
                first.next_cursor,
                first.continuation_receipt,
            )
        self.assertEqual(changed.exception.contract_code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(changed.exception.reason_code, "CONTINUATION_STATE_CHANGED")


class MCPContinuationErrorTests(unittest.TestCase):
    def test_missing_receipt_is_structured_invalid_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"AGENT_RUNTIME_WORKSPACE_ROOT": directory},
            ):
                result = asyncio.run(
                    server.mcp.call_tool(
                        "fs_list",
                        {
                            "cwd": directory,
                            "path": ".",
                            "max_entries": 1,
                            "cursor": "x",
                        },
                    )
                )
        self.assertTrue(result.is_error)
        self.assertEqual(
            result.structured_content["error"]["code"],
            ContractErrorCode.INVALID_ARGUMENT.value,
        )
        self.assertEqual(
            result.structured_content["error"]["reason_code"],
            "CONTINUATION_PAIR_REQUIRED",
        )
        self.assertEqual(
            result.structured_content["error"]["effect_state"],
            "absent",
        )
        self.assertFalse(
            result.structured_content["error"]["reconciliation_required"]
        )


if __name__ == "__main__":
    unittest.main()
