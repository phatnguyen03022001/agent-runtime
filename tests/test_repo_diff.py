from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.contracts import CapabilityFailure
from agent_runtime.repo_diff import (
    CALL_DEADLINE_SECONDS,
    FULL_DIFF_MAX_BYTES,
    GIT_EXECUTABLE,
    REPO_DIFF_CONTRACT,
    RETURNED_PATCH_MAX_BYTES,
    diff_repository,
)
from agent_runtime.tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GIT_EXECUTABLE, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )


class RepoDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = Path(self._temp.name)
        self.repo = self.workspace / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Agent Runtime Test")
        _git(self.repo, "config", "user.email", "agent-runtime@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_contract_exact_authority_annotations_and_bounds(self) -> None:
        self.assertEqual(REPO_DIFF_CONTRACT.name, "repo_diff")
        self.assertIs(REPO_DIFF_CONTRACT.tool_class, ToolClass.REPO)
        self.assertEqual(
            REPO_DIFF_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.NONE),
        )
        self.assertEqual(
            REPO_DIFF_CONTRACT.annotations,
            ToolAnnotations(True, False, True, False),
        )
        self.assertEqual(GIT_EXECUTABLE, "/usr/bin/git")
        self.assertEqual(FULL_DIFF_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(RETURNED_PATCH_MAX_BYTES, 256 * 1024)
        self.assertEqual(CALL_DEADLINE_SECONDS, 5.0)

    def test_worktree_scope_is_tracked_unstaged_relative_to_index(self) -> None:
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("not included\n", encoding="utf-8")
        result = diff_repository(str(self.repo), "worktree")
        self.assertEqual(result.scope, "worktree")
        self.assertIn("-one", result.patch)
        self.assertIn("+two", result.patch)
        self.assertNotIn("untracked.txt", result.patch)
        self.assertFalse(result.network_used)
        self.assertEqual(result.full_diff_bytes, len(result.patch.encode("utf-8")))
        self.assertEqual(result.diff_receipt.kind, "repo-diff")
        self.assertRegex(result.diff_receipt.digest, r"^[0-9a-f]{64}$")

    def test_staged_scope_is_index_relative_to_head(self) -> None:
        (self.repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        result = diff_repository(str(self.repo), "staged")
        self.assertIn("-one", result.patch)
        self.assertIn("+staged", result.patch)
        self.assertEqual(result.scope, "staged")

        worktree = diff_repository(str(self.repo), "worktree")
        self.assertEqual(worktree.patch, "")

    def test_cwd_must_be_exact_repository_root(self) -> None:
        child = self.repo / "child"
        child.mkdir()
        with self.assertRaises(CapabilityFailure) as caught:
            diff_repository(str(child), "worktree")
        self.assertEqual(caught.exception.code, ContractErrorCode.PRECONDITION_FAILED)
        self.assertEqual(caught.exception.reason_code, "NOT_REPOSITORY_ROOT")

    def test_fixed_git_shell_false_and_hardened_diff_flags(self) -> None:
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        real_popen = subprocess.Popen
        with patch("agent_runtime.repo_diff.subprocess.Popen", wraps=real_popen) as popen:
            diff_repository(str(self.repo), "worktree")
        self.assertGreaterEqual(popen.call_count, 4)
        for call in popen.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[0], "/usr/bin/git")
            self.assertIs(call.kwargs["shell"], False)
            self.assertIn("GIT_OPTIONAL_LOCKS", call.kwargs["env"])
            self.assertEqual(call.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(call.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertIn("core.fsmonitor=false", argv)
            self.assertIn("submodule.recurse=false", argv)
        diff_argv = popen.call_args_list[-1].args[0]
        self.assertIn("--no-ext-diff", diff_argv)
        self.assertIn("--no-textconv", diff_argv)
        self.assertIn("--ignore-submodules=all", diff_argv)
        self.assertIn("--no-color", diff_argv)

    def test_receipt_hashes_full_raw_diff_independent_of_display_truncation(self) -> None:
        (self.repo / "tracked.txt").write_text("two-" + ("x" * 2048) + "\n", encoding="utf-8")
        full = diff_repository(str(self.repo), "worktree")
        with patch("agent_runtime.repo_diff.RETURNED_PATCH_MAX_BYTES", 64):
            truncated = diff_repository(str(self.repo), "worktree")
        self.assertFalse(full.patch_truncated)
        self.assertTrue(truncated.patch_truncated)
        self.assertLessEqual(len(truncated.patch.encode("utf-8")), 64)
        self.assertEqual(full.full_diff_bytes, truncated.full_diff_bytes)
        self.assertEqual(full.diff_receipt.digest, truncated.diff_receipt.digest)

    def test_receipt_changes_when_raw_diff_changes(self) -> None:
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        first = diff_repository(str(self.repo), "worktree")
        (self.repo / "tracked.txt").write_text("three\n", encoding="utf-8")
        second = diff_repository(str(self.repo), "worktree")
        self.assertNotEqual(first.diff_receipt.digest, second.diff_receipt.digest)

    def test_full_raw_diff_limit_returns_error_and_no_partial_receipt(self) -> None:
        (self.repo / "tracked.txt").write_text("two-" + ("x" * 4096) + "\n", encoding="utf-8")
        with patch("agent_runtime.repo_diff.FULL_DIFF_MAX_BYTES", 64):
            with self.assertRaises(CapabilityFailure) as caught:
                diff_repository(str(self.repo), "worktree")
        self.assertEqual(caught.exception.code, ContractErrorCode.LIMIT_EXCEEDED)
        self.assertEqual(caught.exception.reason_code, "DIFF_STATE_LIMIT")

    def test_invalid_scope_is_rejected_before_git(self) -> None:
        with self.assertRaises(CapabilityFailure) as caught:
            diff_repository(str(self.repo), "all")
        self.assertEqual(caught.exception.code, ContractErrorCode.INVALID_ARGUMENT)
        self.assertEqual(caught.exception.reason_code, "INVALID_SCOPE")

    def test_outside_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside_raw:
            outside = Path(outside_raw)
            _git(outside, "init", "-q")
            with self.assertRaises(CapabilityFailure) as caught:
                diff_repository(str(outside), "worktree")
        self.assertEqual(caught.exception.code, ContractErrorCode.OUTSIDE_WORKSPACE)


if __name__ == "__main__":
    unittest.main()
