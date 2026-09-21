from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.contracts import CapabilityFailure, RepoStageItem
from agent_runtime.repo_diff import diff_repository
from agent_runtime.repo_stage import (
    CALL_DEADLINE_SECONDS,
    GIT_EXECUTABLE,
    MAX_AGGREGATE_BYTES,
    MAX_FILE_BYTES,
    MAX_ITEMS,
    REPO_STAGE_CONTRACT,
    stage_repository,
)
from agent_runtime.tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
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


class RepoStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = Path(self._temp.name).resolve()
        self.repo = self.workspace / "repo"
        self.origin = self.workspace / "origin.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Fixture User")
        _git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "child.txt").write_text("child\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt", "nested/child.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        _git(self.repo, "branch", "-M", "main")
        _git(self.workspace, "init", "-q", "--bare", str(self.origin))
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace)},
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()

    def _sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _item(
        self,
        path: str,
        operation: str,
        expected_sha256: str | None,
    ) -> RepoStageItem:
        return RepoStageItem(
            path=path,
            operation=operation,
            expected_sha256=expected_sha256,
        )

    def test_contract_exact_authority_annotations_and_bounds(self) -> None:
        self.assertEqual(REPO_STAGE_CONTRACT.name, "repo_stage")
        self.assertIs(REPO_STAGE_CONTRACT.tool_class, ToolClass.REPO)
        self.assertEqual(
            REPO_STAGE_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
        )
        self.assertEqual(
            REPO_STAGE_CONTRACT.annotations,
            ToolAnnotations(False, True, False, False),
        )
        self.assertEqual(MAX_ITEMS, 50)
        self.assertEqual(MAX_FILE_BYTES, 1024 * 1024)
        self.assertEqual(MAX_AGGREGATE_BYTES, 16 * 1024 * 1024)
        self.assertEqual(CALL_DEADLINE_SECONDS, 5.0)

    def test_tracked_utf8_modification_stages_exact_candidate_and_receipt(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [self._item("tracked.txt", "present", self._sha(target))],
        )
        self.assertEqual(result.head_sha, head)
        self.assertEqual(self._head(), head)
        self.assertEqual([item.path for item in result.staged_paths], ["tracked.txt"])
        self.assertTrue(result.post_stage_clean)
        self.assertFalse(result.network_used)
        immediate = diff_repository(str(self.repo), "staged")
        self.assertEqual(
            result.staged_diff_receipt.model_dump(),
            immediate.diff_receipt.model_dump(),
        )
        self.assertIn("+two", immediate.patch)
        self.assertEqual(diff_repository(str(self.repo), "worktree").full_diff_bytes, 0)

    def test_new_regular_utf8_file_stages(self) -> None:
        head = self._head()
        target = self.repo / "new.txt"
        target.write_text("new\n", encoding="utf-8")
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [self._item("new.txt", "present", self._sha(target))],
        )
        staged = result.staged_paths[0]
        self.assertEqual(staged.operation, "present")
        self.assertEqual(staged.worktree_sha256, self._sha(target))
        self.assertRegex(staged.git_blob_sha or "", r"^[0-9a-f]{40}$")
        self.assertEqual(staged.git_mode, "100644")

    def test_tracked_delete_stages(self) -> None:
        head = self._head()
        (self.repo / "tracked.txt").unlink()
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [self._item("tracked.txt", "delete", None)],
        )
        staged = result.staged_paths[0]
        self.assertEqual(staged.operation, "delete")
        self.assertIsNone(staged.worktree_sha256)
        self.assertIsNone(staged.git_blob_sha)
        self.assertIsNone(staged.git_mode)
        self.assertIn("deleted file mode", diff_repository(str(self.repo), "staged").patch)

    def test_nested_delete_allows_deleted_parent_directory(self) -> None:
        head = self._head()
        (self.repo / "nested" / "child.txt").unlink()
        (self.repo / "nested").rmdir()
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [self._item("nested/child.txt", "delete", None)],
        )
        self.assertTrue(result.post_stage_clean)

    def test_executable_bit_change_stages_as_100755(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.chmod(target.stat().st_mode | 0o111)
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [self._item("tracked.txt", "present", self._sha(target))],
        )
        self.assertEqual(result.staged_paths[0].git_mode, "100755")
        patch_text = diff_repository(str(self.repo), "staged").patch
        self.assertIn("old mode 100644", patch_text)
        self.assertIn("new mode 100755", patch_text)

    def test_rename_is_explicit_delete_plus_present(self) -> None:
        head = self._head()
        old = self.repo / "tracked.txt"
        new = self.repo / "renamed.txt"
        old.rename(new)
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [
                self._item("tracked.txt", "delete", None),
                self._item("renamed.txt", "present", self._sha(new)),
            ],
        )
        self.assertEqual(
            {item.path for item in result.staged_paths},
            {"tracked.txt", "renamed.txt"},
        )
        self.assertTrue(result.post_stage_clean)

    def test_duplicate_path_rejected_before_index_mutation(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        item = self._item("tracked.txt", "present", self._sha(target))
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(str(self.repo), "main", head, [item, item])
        self.assertEqual(caught.exception.reason_code, "DUPLICATE_PATH")
        self.assertEqual(diff_repository(str(self.repo), "staged").full_diff_bytes, 0)

    def test_unselected_candidate_path_rejected(self) -> None:
        head = self._head()
        first = self.repo / "tracked.txt"
        second = self.repo / "other.txt"
        first.write_text("two\n", encoding="utf-8")
        second.write_text("other\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "present", self._sha(first))],
            )
        self.assertEqual(caught.exception.reason_code, "UNSELECTED_CHANGES_PRESENT")
        self.assertEqual(diff_repository(str(self.repo), "staged").full_diff_bytes, 0)

    def test_initial_staged_diff_must_be_empty(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "present", self._sha(target))],
            )
        self.assertEqual(caught.exception.reason_code, "STAGED_CHANGES_PRESENT")

    def test_present_expected_sha_is_mandatory_and_exact(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "present", "0" * 64)],
            )
        self.assertEqual(caught.exception.reason_code, "EXPECTED_SHA_MISMATCH")

    def test_delete_expected_sha_is_forbidden(self) -> None:
        head = self._head()
        (self.repo / "tracked.txt").unlink()
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "delete", "0" * 64)],
            )
        self.assertEqual(caught.exception.reason_code, "EXPECTED_SHA_FORBIDDEN")

    def test_ignored_untracked_candidate_is_rejected_explicitly(self) -> None:
        head = self._head()
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-q", "-m", "ignore")
        _git(self.repo, "push", "-q")
        head = self._head()
        target = self.repo / "ignored.txt"
        target.write_text("ignored\n", encoding="utf-8")
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("ignored.txt", "present", self._sha(target))],
            )
        self.assertEqual(caught.exception.reason_code, "IGNORED_PATH")

    def test_check_ignore_transports_candidate_path_via_nul_stdin(self) -> None:
        import agent_runtime.repo_stage as module

        candidate = "literal:[*?].txt"
        with patch.object(module, "_run_git") as run:
            run.return_value.returncode = 1
            ignored = module._is_ignored(self.repo, candidate, 123.0)

        self.assertFalse(ignored)
        run.assert_called_once()
        argv = run.call_args.args[1]
        self.assertEqual(argv, ["check-ignore", "--stdin", "-z"])
        self.assertNotIn(candidate, argv)
        self.assertEqual(
            run.call_args.kwargs["stdin"],
            candidate.encode("utf-8", errors="strict") + b"\x00",
        )
        self.assertEqual(run.call_args.kwargs["deadline"], 123.0)
        check_env = run.call_args.kwargs["env"]
        self.assertNotIn("GIT_LITERAL_PATHSPECS", check_env)
        self.assertNotIn("GIT_NOGLOB_PATHSPECS", check_env)
        self.assertNotIn("GIT_GLOB_PATHSPECS", check_env)
        self.assertNotIn("GIT_ICASE_PATHSPECS", check_env)

    def test_regular_run_git_keeps_literal_pathspec_guard_by_default(self) -> None:
        import agent_runtime.repo_stage as module

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        with patch.object(module.subprocess, "run", return_value=completed) as run:
            result = module._run_git(
                self.repo,
                ["rev-parse", "--is-inside-work-tree"],
                deadline=time.monotonic() + 1.0,
            )

        self.assertEqual(result.returncode, 0)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.kwargs["env"]["GIT_LITERAL_PATHSPECS"],
            "1",
        )

    def test_pathspec_looking_untracked_filename_is_treated_literally(self) -> None:
        head = self._head()
        path = "literal:[*?].txt"
        target = self.repo / path
        target.write_text("literal\n", encoding="utf-8")
        result = stage_repository(
            str(self.repo),
            "main",
            head,
            [self._item(path, "present", self._sha(target))],
        )
        self.assertEqual([item.path for item in result.staged_paths], [path])
        self.assertTrue(result.post_stage_clean)
        immediate = diff_repository(str(self.repo), "staged")
        self.assertIn(path, immediate.patch)
        self.assertEqual(
            result.staged_diff_receipt.model_dump(),
            immediate.diff_receipt.model_dump(),
        )

    def test_binary_nul_candidate_rejected(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_bytes(b"a\x00b")
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "present", self._sha(target))],
            )
        self.assertEqual(caught.exception.reason_code, "BINARY_CONTENT")

    def test_invalid_utf8_candidate_rejected(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_bytes(b"\xff")
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "present", self._sha(target))],
            )
        self.assertEqual(caught.exception.reason_code, "INVALID_UTF8")

    def test_symlink_candidate_rejected(self) -> None:
        head = self._head()
        target = self.repo / "link.txt"
        target.symlink_to("tracked.txt")
        digest = hashlib.sha256((self.repo / "tracked.txt").read_bytes()).hexdigest()
        with self.assertRaises(CapabilityFailure) as caught:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("link.txt", "present", digest)],
            )
        self.assertEqual(caught.exception.reason_code, "SYMLINK_DISALLOWED")

    def test_single_index_info_mutation_and_no_git_add(self) -> None:
        head = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        import agent_runtime.repo_stage as module

        real_run = subprocess.run
        with patch.object(module.subprocess, "run", wraps=real_run) as run:
            stage_repository(
                str(self.repo),
                "main",
                head,
                [self._item("tracked.txt", "present", self._sha(target))],
            )
        argvs = [call.args[0] for call in run.call_args_list]
        commands = [argv[argv.index("--no-pager") + 1 :] for argv in argvs]
        self.assertFalse(any("add" in command for command in commands))
        update_calls = [argv for argv in argvs if "update-index" in argv]
        self.assertEqual(len(update_calls), 1)
        self.assertIn("--index-info", update_calls[0])
        hash_calls = [call for call in run.call_args_list if "hash-object" in call.args[0]]
        self.assertEqual(len(hash_calls), 1)
        self.assertEqual(hash_calls[0].kwargs["input"], b"two\n")


if __name__ == "__main__":
    unittest.main()
