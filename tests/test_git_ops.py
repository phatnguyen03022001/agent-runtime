from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_runtime.config import ProjectProfile
from agent_runtime.git_ops import GitError, inspect_repository, sync_checkout


def run(cwd: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stdout)
    return result.stdout.strip()


class GitFixture:
    def __init__(self, case: unittest.TestCase):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.checkout = root / "checkout"
        run(root, "git", "init", "--bare", str(self.remote))
        run(root, "git", "init", "-b", "main", str(self.checkout))
        run(self.checkout, "git", "config", "user.name", "Runtime Tests")
        run(self.checkout, "git", "config", "user.email", "runtime@example.invalid")
        (self.checkout / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
        (self.checkout / "tracked.txt").write_text("base\n", encoding="utf-8")
        run(self.checkout, "git", "add", ".")
        run(self.checkout, "git", "commit", "-m", "base")
        run(self.checkout, "git", "remote", "add", "origin", str(self.remote))
        run(self.checkout, "git", "push", "-u", "origin", "main")
        self.profile = ProjectProfile(
            project_id="example-main",
            repository="owner/repo",
            checkout=self.checkout.resolve(),
            remote="origin",
            expected_remote_url=str(self.remote),
            branch="main",
            verify_argv=("./verify",),
            timeout_seconds=30,
            disposable=True,
        )


class GitOpsTests(unittest.TestCase):
    def test_inspect_reports_head_branch_clean_and_cached_remote(self) -> None:
        fx = GitFixture(self)
        state = inspect_repository(fx.profile)
        self.assertTrue(state["ok"])
        self.assertEqual(state["current_branch"], "main")
        self.assertTrue(state["clean"])
        self.assertEqual(state["head"], state["cached_remote_head"])
        self.assertTrue(state["in_sync"])
        self.assertTrue(state["remote_identity_ok"])
        self.assertNotIn(str(fx.checkout), repr(state))

    def test_remote_mismatch_rejected(self) -> None:
        fx = GitFixture(self)
        run(fx.checkout, "git", "remote", "set-url", "origin", str(fx.remote) + "-wrong")
        with self.assertRaises(GitError):
            sync_checkout(fx.profile)

    def test_sync_resets_tracked_changes_deletes_untracked_preserves_ignored(self) -> None:
        fx = GitFixture(self)
        (fx.checkout / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (fx.checkout / "untracked.txt").write_text("delete me\n", encoding="utf-8")
        (fx.checkout / "ignored.tmp").write_text("keep me\n", encoding="utf-8")
        state = sync_checkout(fx.profile)
        self.assertTrue(state["ok"])
        self.assertEqual((fx.checkout / "tracked.txt").read_text(), "base\n")
        self.assertFalse((fx.checkout / "untracked.txt").exists())
        self.assertEqual((fx.checkout / "ignored.tmp").read_text(), "keep me\n")
        self.assertTrue(state["clean"])

    def test_sync_requires_explicit_disposable_profile(self) -> None:
        fx = GitFixture(self)
        safe = ProjectProfile(**{**fx.profile.__dict__, "disposable": False})
        with self.assertRaises(GitError):
            sync_checkout(safe)

    def test_runtime_git_api_contains_no_push_commit_branch_creation_or_fdx(self) -> None:
        source = (Path(__file__).parents[1] / "agent_runtime" / "git_ops.py").read_text(encoding="utf-8")
        self.assertNotIn('"push"', source)
        self.assertNotIn('"commit"', source)
        self.assertNotIn('"branch", "-c"', source)
        self.assertNotIn('"checkout", "-b"', source)
        self.assertNotIn("clean\", \"-fdx", source)
        self.assertIn('"clean", "-fd"', source)


if __name__ == "__main__":
    unittest.main()
