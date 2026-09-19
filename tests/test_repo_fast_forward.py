from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import repo_fast_forward as module
from agent_runtime.repo_fast_forward import (
    CALL_DEADLINE_SECONDS,
    GIT_EXECUTABLE,
    RepoFastForwardFailure,
    fast_forward_repository,
)

GIT = "/usr/bin/git"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        [GIT, *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


class RepoFastForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0090-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.local = self.root / "local"

        _git(self.root, "init", "-q", "--bare", str(self.remote))
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "dev")
        _git(self.seed, "config", "user.name", "Task 0090")
        _git(self.seed, "config", "user.email", "task0090@example.invalid")
        (self.seed / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.seed, "add", "tracked.txt")
        _git(self.seed, "commit", "-q", "-m", "initial")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-q", "-u", "origin", "dev")
        _git(self.root, "clone", "-q", "-b", "dev", str(self.remote), str(self.local))
        _git(self.local, "config", "user.name", "Task 0090")
        _git(self.local, "config", "user.email", "task0090@example.invalid")

        self.expected_local = _git(self.local, "rev-parse", "HEAD")
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _advance_remote(self, text: str = "two\n") -> str:
        (self.seed / "tracked.txt").write_text(text, encoding="utf-8")
        _git(self.seed, "commit", "-qam", f"remote {text.strip()}")
        _git(self.seed, "push", "-q", "origin", "dev")
        return _git(self.seed, "rev-parse", "HEAD")

    def _call(
        self,
        *,
        branch: str = "dev",
        expected_local: str | None = None,
        expected_remote: str | None = None,
        cwd: Path | None = None,
    ):
        remote = expected_remote or self._advance_remote()
        return fast_forward_repository(
            str(cwd or self.local),
            branch,
            expected_local or self.expected_local,
            remote,
        )

    def _assert_failure(self, code: str, callback) -> RepoFastForwardFailure:
        with self.assertRaises(RepoFastForwardFailure) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertLessEqual(len(raised.exception.message), 256)
        self.assertNotIn(str(self.remote), raised.exception.message)
        return raised.exception

    def test_successful_clean_fast_forward_has_closed_proof(self) -> None:
        expected_remote = self._advance_remote()
        result = self._call(expected_remote=expected_remote)
        self.assertEqual(result.status, "fast_forwarded")
        self.assertEqual(result.repository_root, str(self.local.resolve()))
        self.assertEqual(result.branch, "dev")
        self.assertEqual(result.remote, "origin")
        self.assertEqual(result.upstream, "origin/dev")
        self.assertEqual(result.expected_local_head, self.expected_local)
        self.assertEqual(result.expected_remote_head, expected_remote)
        self.assertEqual(result.head_before, self.expected_local)
        self.assertEqual(result.head_after, expected_remote)
        self.assertEqual(result.tracking_head, expected_remote)
        self.assertTrue(result.fetched)
        self.assertTrue(result.network_used)
        self.assertTrue(result.fast_forwarded)
        self.assertEqual(result.deadline_seconds, CALL_DEADLINE_SECONDS)
        self.assertEqual(_git(self.local, "status", "--porcelain"), "")

    def test_exact_replay_fetches_and_returns_already_at_target(self) -> None:
        expected_remote = self._advance_remote()
        first = self._call(expected_remote=expected_remote)
        before = _git(self.local, "rev-parse", "HEAD")
        replay = self._call(expected_remote=expected_remote)
        self.assertEqual(first.status, "fast_forwarded")
        self.assertEqual(replay.status, "already_at_target")
        self.assertEqual(replay.head_before, expected_remote)
        self.assertEqual(replay.head_after, expected_remote)
        self.assertFalse(replay.fast_forwarded)
        self.assertTrue(replay.fetched)
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), before)

    def test_remote_later_advanced_is_mismatch_without_local_movement(self) -> None:
        expected_remote = self._advance_remote()
        self._call(expected_remote=expected_remote)
        stable_head = _git(self.local, "rev-parse", "HEAD")
        self._advance_remote("three\n")
        self._assert_failure(
            "REMOTE_HEAD_MISMATCH",
            lambda: self._call(expected_remote=expected_remote),
        )
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), stable_head)

    def test_divergent_history_is_non_fast_forward_without_movement(self) -> None:
        (self.local / "local-only.txt").write_text("local\n", encoding="utf-8")
        _git(self.local, "add", "local-only.txt")
        _git(self.local, "commit", "-q", "-m", "local divergence")
        local_diverged = _git(self.local, "rev-parse", "HEAD")
        expected_remote = self._advance_remote()
        self._assert_failure(
            "NON_FAST_FORWARD",
            lambda: self._call(
                expected_local=local_diverged,
                expected_remote=expected_remote,
            ),
        )
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), local_diverged)

    def test_dirty_tracked_staged_and_untracked_reject_before_fetch(self) -> None:
        cases = ("tracked", "staged", "untracked")
        for case in cases:
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                expected_remote = self._advance_remote()
                if case == "tracked":
                    (self.local / "tracked.txt").write_text("dirty\n", encoding="utf-8")
                elif case == "staged":
                    (self.local / "tracked.txt").write_text("staged\n", encoding="utf-8")
                    _git(self.local, "add", "tracked.txt")
                else:
                    (self.local / "untracked.txt").write_text("u\n", encoding="utf-8")
                tracking_before = _git(self.local, "rev-parse", "origin/dev")
                self._assert_failure(
                    "DIRTY_WORKTREE",
                    lambda: self._call(expected_remote=expected_remote),
                )
                self.assertEqual(_git(self.local, "rev-parse", "origin/dev"), tracking_before)

    def test_in_progress_operation_rejects_before_fetch(self) -> None:
        expected_remote = self._advance_remote()
        git_dir = Path(_git(self.local, "rev-parse", "--absolute-git-dir"))
        (git_dir / "MERGE_HEAD").write_text(self.expected_local + "\n", encoding="ascii")
        self._assert_failure(
            "OPERATION_IN_PROGRESS",
            lambda: self._call(expected_remote=expected_remote),
        )

    def test_subdirectory_detached_branch_upstream_and_third_head_reject(self) -> None:
        expected_remote = self._advance_remote()

        nested = self.local / "nested"
        nested.mkdir()
        self._assert_failure(
            "NOT_REPOSITORY_ROOT",
            lambda: self._call(expected_remote=expected_remote, cwd=nested),
        )

        _git(self.local, "checkout", "-q", "--detach", self.expected_local)
        self._assert_failure(
            "DETACHED_HEAD",
            lambda: self._call(expected_remote=expected_remote),
        )
        _git(self.local, "checkout", "-q", "dev")

        self._assert_failure(
            "BRANCH_MISMATCH",
            lambda: self._call(branch="main", expected_remote=expected_remote),
        )

        _git(self.local, "branch", "--unset-upstream")
        self._assert_failure(
            "UPSTREAM_MISMATCH",
            lambda: self._call(expected_remote=expected_remote),
        )
        _git(self.local, "branch", "--set-upstream-to=origin/dev", "dev")

        (self.local / "third.txt").write_text("third\n", encoding="utf-8")
        _git(self.local, "add", "third.txt")
        _git(self.local, "commit", "-q", "-m", "third local head")
        third = _git(self.local, "rev-parse", "HEAD")
        self._assert_failure(
            "LOCAL_HEAD_MISMATCH",
            lambda: self._call(expected_remote=expected_remote),
        )
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), third)

    def test_local_state_change_after_fetch_fails_closed(self) -> None:
        expected_remote = self._advance_remote()
        real_run = module._run_git

        def racing_run(*args, **kwargs):
            result = real_run(*args, **kwargs)
            git_args = args[1]
            if git_args and git_args[0] == "fetch" and result.returncode == 0:
                (self.local / "raced.txt").write_text("race\n", encoding="utf-8")
            return result

        with patch.object(module, "_run_git", side_effect=racing_run):
            self._assert_failure(
                "LOCAL_STATE_CHANGED",
                lambda: self._call(expected_remote=expected_remote),
            )
        self.assertEqual(_git(self.local, "rev-parse", "HEAD"), self.expected_local)

    def test_git_invocations_are_fixed_non_shell_and_forbidden_verbs_absent(self) -> None:
        expected_remote = self._advance_remote()
        real_popen = subprocess.Popen
        calls: list[tuple[list[str], dict[str, object]]] = []

        def recording_popen(argv, *args, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return real_popen(argv, *args, **kwargs)

        with patch("agent_runtime.repo_fast_forward.subprocess.Popen", side_effect=recording_popen):
            self._call(expected_remote=expected_remote)

        self.assertGreater(len(calls), 8)
        forbidden = {
            "pull",
            "push",
            "reset",
            "rebase",
            "checkout",
            "switch",
            "stash",
            "clean",
            "cherry-pick",
            "commit",
        }
        for argv, kwargs in calls:
            self.assertEqual(argv[0], GIT_EXECUTABLE)
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(forbidden.intersection(argv), argv)

        fetch = next(argv for argv, _ in calls if "fetch" in argv)
        self.assertIn("--no-tags", fetch)
        self.assertIn("--no-recurse-submodules", fetch)
        self.assertIn("origin", fetch)
        self.assertIn(
            "refs/heads/dev:refs/remotes/origin/dev",
            fetch,
        )
        merge = next(argv for argv, _ in calls if "merge" in argv)
        self.assertIn("--ff-only", merge)
        self.assertIn("--no-edit", merge)
        self.assertIn("core.hooksPath=/dev/null", merge)
        self.assertIn("submodule.recurse=false", merge)


if __name__ == "__main__":
    unittest.main()
