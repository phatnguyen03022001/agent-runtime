from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.contracts import CapabilityFailure, RepoStageItem
from agent_runtime.repo_commit import (
    CALL_DEADLINE_SECONDS,
    GIT_EXECUTABLE,
    MAX_AGGREGATE_STAGED_BYTES,
    MAX_BLOB_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_STAGED_PATHS,
    REPO_COMMIT_CONTRACT,
    commit_repository,
)
from agent_runtime.repo_diff import diff_repository
from agent_runtime.repo_stage import stage_repository
from agent_runtime.tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    make_receipt_v1,
)


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GIT_EXECUTABLE, *args],
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )


class RepoCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = Path(self._temp.name).resolve()
        self.repo = self.workspace / "repo"
        self.origin = self.workspace / "origin.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Repository Fallback")
        _git(self.repo, "config", "user.email", "fallback@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        _git(self.repo, "branch", "-M", "main")
        _git(self.workspace, "init", "-q", "--bare", str(self.origin))
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace),
                "AGENT_RUNTIME_GIT_NAME": "Agent Runtime",
                "AGENT_RUNTIME_GIT_EMAIL": "runtime@example.invalid",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()

    def _stage_change(self, text: str = "two\n"):
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        staged = stage_repository(
            str(self.repo),
            "main",
            head,
            [
                RepoStageItem(
                    path="tracked.txt",
                    operation="present",
                    expected_sha256=digest,
                )
            ],
        )
        return head, staged

    def test_contract_exact_authority_annotations_and_bounds(self) -> None:
        self.assertEqual(REPO_COMMIT_CONTRACT.name, "repo_commit")
        self.assertIs(REPO_COMMIT_CONTRACT.tool_class, ToolClass.REPO)
        self.assertEqual(
            REPO_COMMIT_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
        )
        self.assertEqual(
            REPO_COMMIT_CONTRACT.annotations,
            ToolAnnotations(False, True, False, False),
        )
        self.assertEqual(MAX_MESSAGE_BYTES, 16 * 1024)
        self.assertEqual(MAX_STAGED_PATHS, 50)
        self.assertEqual(MAX_BLOB_BYTES, 1024 * 1024)
        self.assertEqual(MAX_AGGREGATE_STAGED_BYTES, 16 * 1024 * 1024)
        self.assertEqual(CALL_DEADLINE_SECONDS, 5.0)

    def test_success_commits_only_staged_state_and_leaves_clean_repository(self) -> None:
        parent, staged = self._stage_change()
        result = commit_repository(
            str(self.repo),
            "main",
            parent,
            staged.staged_diff_receipt,
            "commit message",
        )
        self.assertEqual(result.parent_sha, parent)
        self.assertEqual(self._head(), result.commit_sha)
        self.assertRegex(result.tree_sha, r"^[0-9a-f]{40}$")
        self.assertRegex(result.commit_sha, r"^[0-9a-f]{40}$")
        self.assertEqual(
            result.diff_receipt.model_dump(),
            staged.staged_diff_receipt.model_dump(),
        )
        self.assertEqual(result.commit_receipt.kind, "repo-commit")
        self.assertRegex(result.commit_receipt.digest, r"^[0-9a-f]{64}$")
        self.assertTrue(result.post_commit_clean)
        self.assertFalse(result.network_used)
        self.assertEqual(diff_repository(str(self.repo), "staged").full_diff_bytes, 0)
        self.assertEqual(diff_repository(str(self.repo), "worktree").full_diff_bytes, 0)
        self.assertEqual(
            _git(self.repo, "status", "--porcelain", "--untracked-files=all").stdout,
            b"",
        )

    def test_commit_has_exactly_one_expected_parent(self) -> None:
        parent, staged = self._stage_change()
        result = commit_repository(
            str(self.repo),
            "main",
            parent,
            staged.staged_diff_receipt,
            "single parent",
        )
        parents = _git(self.repo, "show", "-s", "--format=%P", result.commit_sha).stdout.decode().strip()
        self.assertEqual(parents, parent)

    def test_runtime_identity_is_author_and_committer_not_repository_fallback(self) -> None:
        parent, staged = self._stage_change()
        result = commit_repository(
            str(self.repo),
            "main",
            parent,
            staged.staged_diff_receipt,
            "identity",
        )
        identity = _git(
            self.repo,
            "show",
            "-s",
            "--format=%an <%ae>|%cn <%ce>",
            result.commit_sha,
        ).stdout.decode().strip()
        self.assertEqual(
            identity,
            "Agent Runtime <runtime@example.invalid>|Agent Runtime <runtime@example.invalid>",
        )

    def test_missing_runtime_identity_fails_before_commit_object_creation(self) -> None:
        parent, staged = self._stage_change()
        with patch.dict(os.environ, {}, clear=False):
            old_name = os.environ.pop("AGENT_RUNTIME_GIT_NAME", None)
            old_email = os.environ.pop("AGENT_RUNTIME_GIT_EMAIL", None)
            try:
                with self.assertRaises(CapabilityFailure) as caught:
                    commit_repository(
                        str(self.repo),
                        "main",
                        parent,
                        staged.staged_diff_receipt,
                        "identity unavailable",
                    )
            finally:
                if old_name is not None:
                    os.environ["AGENT_RUNTIME_GIT_NAME"] = old_name
                if old_email is not None:
                    os.environ["AGENT_RUNTIME_GIT_EMAIL"] = old_email
        self.assertEqual(caught.exception.code, ContractErrorCode.UNAVAILABLE)
        self.assertEqual(caught.exception.reason_code, "GIT_IDENTITY_UNAVAILABLE")
        self.assertEqual(self._head(), parent)

    def test_invalid_runtime_identity_with_newline_is_rejected(self) -> None:
        parent, staged = self._stage_change()
        with patch.dict(
            os.environ,
            {"AGENT_RUNTIME_GIT_NAME": "bad\nname"},
            clear=False,
        ):
            with self.assertRaises(CapabilityFailure) as caught:
                commit_repository(
                    str(self.repo),
                    "main",
                    parent,
                    staged.staged_diff_receipt,
                    "identity invalid",
                )
        self.assertEqual(caught.exception.reason_code, "GIT_IDENTITY_UNAVAILABLE")
        self.assertEqual(self._head(), parent)

    def test_expected_staged_receipt_is_exact(self) -> None:
        parent, staged = self._stage_change()
        wrong = staged.staged_diff_receipt.model_copy(update={"digest": "0" * 64})
        with self.assertRaises(CapabilityFailure) as caught:
            commit_repository(
                str(self.repo),
                "main",
                parent,
                wrong,
                "wrong receipt",
            )
        self.assertEqual(caught.exception.reason_code, "DIFF_RECEIPT_MISMATCH")
        self.assertEqual(self._head(), parent)

    def test_empty_staged_state_rejected(self) -> None:
        parent = self._head()
        empty = diff_repository(str(self.repo), "staged").diff_receipt
        with self.assertRaises(CapabilityFailure) as caught:
            commit_repository(str(self.repo), "main", parent, empty, "nothing")
        self.assertEqual(caught.exception.reason_code, "NO_STAGED_CHANGES")

    def test_unstaged_change_after_stage_is_rejected(self) -> None:
        parent, staged = self._stage_change()
        (self.repo / "tracked.txt").write_text("three\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as caught:
            commit_repository(
                str(self.repo),
                "main",
                parent,
                staged.staged_diff_receipt,
                "unstaged",
            )
        self.assertEqual(caught.exception.reason_code, "UNSTAGED_CHANGES_PRESENT")
        self.assertEqual(self._head(), parent)

    def test_untracked_change_after_stage_is_rejected(self) -> None:
        parent, staged = self._stage_change()
        (self.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as caught:
            commit_repository(
                str(self.repo),
                "main",
                parent,
                staged.staged_diff_receipt,
                "untracked",
            )
        self.assertEqual(caught.exception.reason_code, "UNTRACKED_CHANGES_PRESENT")
        self.assertEqual(self._head(), parent)

    def test_binary_staged_blob_is_rejected(self) -> None:
        parent = self._head()
        (self.repo / "binary.bin").write_bytes(b"a\x00b")
        _git(self.repo, "add", "binary.bin")
        receipt = diff_repository(str(self.repo), "staged").diff_receipt
        with self.assertRaises(CapabilityFailure) as caught:
            commit_repository(str(self.repo), "main", parent, receipt, "binary")
        self.assertEqual(caught.exception.reason_code, "BINARY_STAGED_CONTENT")
        self.assertEqual(self._head(), parent)

    def test_symlink_staged_entry_is_rejected(self) -> None:
        parent = self._head()
        (self.repo / "link.txt").symlink_to("tracked.txt")
        _git(self.repo, "add", "link.txt")
        receipt = diff_repository(str(self.repo), "staged").diff_receipt
        with self.assertRaises(CapabilityFailure) as caught:
            commit_repository(str(self.repo), "main", parent, receipt, "symlink")
        self.assertEqual(caught.exception.reason_code, "UNSUPPORTED_STAGED_TYPE")
        self.assertEqual(self._head(), parent)

    def test_commit_tree_message_and_update_ref_expected_old_are_exact(self) -> None:
        parent, staged = self._stage_change()
        import agent_runtime.repo_commit as module

        real_run = subprocess.run
        with patch.object(module.subprocess, "run", wraps=real_run) as run:
            result = commit_repository(
                str(self.repo),
                "main",
                parent,
                staged.staged_diff_receipt,
                "exact bytes",
            )
        commit_calls = [call for call in run.call_args_list if "commit-tree" in call.args[0]]
        self.assertEqual(len(commit_calls), 1)
        self.assertEqual(commit_calls[0].kwargs["input"], b"exact bytes")
        self.assertEqual(
            commit_calls[0].kwargs["env"]["GIT_AUTHOR_NAME"],
            "Agent Runtime",
        )
        self.assertEqual(
            commit_calls[0].kwargs["env"]["GIT_COMMITTER_EMAIL"],
            "runtime@example.invalid",
        )
        update_calls = [call for call in run.call_args_list if "update-ref" in call.args[0]]
        self.assertEqual(len(update_calls), 1)
        argv = update_calls[0].args[0]
        index = argv.index("update-ref")
        self.assertEqual(
            argv[index:index + 4],
            ["update-ref", "refs/heads/main", result.commit_sha, parent],
        )
        forbidden = {"commit", "add", "push", "fetch", "pull", "merge", "rebase", "reset", "checkout", "stash", "clean"}
        commands = {
            argv[next(i for i, value in enumerate(argv) if value in {
                "check-ref-format", "rev-parse", "symbolic-ref", "ls-files", "diff",
                "ls-tree", "cat-file", "write-tree", "commit-tree", "update-ref"
            })]
            for argv in (call.args[0] for call in run.call_args_list)
            if any(value in argv for value in {
                "check-ref-format", "rev-parse", "symbolic-ref", "ls-files", "diff",
                "ls-tree", "cat-file", "write-tree", "commit-tree", "update-ref"
            })
        }
        self.assertTrue(commands.isdisjoint(forbidden))

    def test_commit_receipt_recomputes_from_exact_commit_object_and_semantics(self) -> None:
        parent, staged = self._stage_change()
        message = "receipt message"
        result = commit_repository(
            str(self.repo),
            "main",
            parent,
            staged.staged_diff_receipt,
            message,
        )
        observed = _git(self.repo, "cat-file", "commit", result.commit_sha).stdout
        expected = make_receipt_v1(
            kind="repo-commit",
            subject={
                "repository_root": str(self.repo.resolve()),
                "branch": "main",
                "commit_sha": result.commit_sha,
            },
            semantic_parameters={
                "parent_sha": parent,
                "expected_diff_receipt_digest": staged.staged_diff_receipt.digest,
                "sha256_of_exact_commit_message_bytes": hashlib.sha256(
                    message.encode("utf-8")
                ).hexdigest(),
            },
            observed_state_bytes=observed,
        )
        self.assertEqual(result.commit_receipt.digest, expected.digest)


if __name__ == "__main__":
    unittest.main()
