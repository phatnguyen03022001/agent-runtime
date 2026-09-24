from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_runtime.repo_publish as module
from agent_runtime.contracts import RepoStageItem
from agent_runtime.fs_write import write_file
from agent_runtime.repo_commit import commit_repository
from agent_runtime.repo_diff import diff_repository
from agent_runtime.repo_fast_forward import RepoFastForwardFailure
from agent_runtime.repo_publish import (
    RepoPublishFailure,
    publish_repository,
)
from agent_runtime.repo_stage import stage_repository

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


class RepoPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0094-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.local = self.root / "local"

        _git(self.root, "init", "-q", "--bare", str(self.remote))
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "dev")
        _git(self.seed, "config", "user.name", "Task 0094")
        _git(self.seed, "config", "user.email", "task0094@example.invalid")
        (self.seed / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.seed, "add", "tracked.txt")
        _git(self.seed, "commit", "-q", "-m", "initial")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-q", "-u", "origin", "dev")
        _git(
            self.root,
            "clone",
            "-q",
            "-b",
            "dev",
            str(self.remote),
            str(self.local),
        )
        _git(self.local, "config", "user.name", "Task 0094")
        _git(self.local, "config", "user.email", "task0094@example.invalid")

        self.expected_remote = _git(self.local, "rev-parse", "HEAD")
        (self.local / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        _git(self.local, "commit", "-qam", "candidate")
        self.commit = _git(self.local, "rev-parse", "HEAD")

        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root),
                "AGENT_RUNTIME_GIT_NAME": "Agent Runtime",
                "AGENT_RUNTIME_GIT_EMAIL": "runtime@example.invalid",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _call(self):
        return publish_repository(
            str(self.local),
            "dev",
            self.expected_remote,
            self.commit,
        )

    def _assert_failure(self, code: str, callback) -> RepoPublishFailure:
        with self.assertRaises(RepoPublishFailure) as raised:
            callback()
        reason_code = getattr(raised.exception, "reason_code", raised.exception.code)
        self.assertEqual(reason_code, code)
        self.assertLessEqual(len(raised.exception.message), 256)
        self.assertNotIn(str(self.remote), raised.exception.message)
        return raised.exception

    def _remote_head(self) -> str:
        output = _git(
            self.local,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/dev",
        )
        return output.split()[0] if output else ""

    def _advance_remote(self, text: str = "remote\n") -> str:
        (self.seed / "tracked.txt").write_text(text, encoding="utf-8")
        _git(self.seed, "commit", "-qam", f"remote {text.strip()}")
        _git(self.seed, "push", "-q", "origin", "dev")
        return _git(self.seed, "rev-parse", "HEAD")
    def test_success_publishes_exact_direct_child_with_closed_proof(self) -> None:
        result = self._call()
        self.assertEqual(result.status, "published")
        self.assertEqual(result.repository_root, str(self.local.resolve()))
        self.assertEqual(result.branch, "dev")
        self.assertEqual(result.remote, "origin")
        self.assertEqual(result.upstream, "origin/dev")
        self.assertEqual(result.expected_remote_head, self.expected_remote)
        self.assertEqual(result.commit, self.commit)
        self.assertEqual(result.head, self.commit)
        self.assertEqual(result.remote_head_before, self.expected_remote)
        self.assertEqual(result.remote_head_after, self.commit)
        self.assertTrue(result.network_used)
        self.assertTrue(result.push_attempted)
        self.assertTrue(result.published)
        self.assertEqual(result.deadline_seconds, module.CALL_DEADLINE_SECONDS)
        self.assertEqual(self._remote_head(), self.commit)

    def test_exact_replay_freshly_observes_and_does_not_push_twice(self) -> None:
        with patch.object(module, "_push_once", wraps=module._push_once) as push:
            first = self._call()
            replay = self._call()
        self.assertEqual(first.status, "published")
        self.assertEqual(replay.status, "already_published")
        self.assertFalse(replay.push_attempted)
        self.assertFalse(replay.published)
        self.assertEqual(push.call_count, 1)
    def test_stale_or_missing_remote_branch_fails_before_push(self) -> None:
        stale = self._advance_remote()
        with patch.object(module, "_push_once", wraps=module._push_once) as push:
            self._assert_failure("REMOTE_HEAD_MISMATCH", self._call)
            self.assertEqual(push.call_count, 0)
        self.assertEqual(self._remote_head(), stale)

        _git(self.seed, "push", "-q", "origin", ":dev")
        self._assert_failure("REMOTE_HEAD_MISMATCH", self._call)

    def test_tracking_head_mismatch_fails_before_push(self) -> None:
        _git(
            self.local,
            "update-ref",
            "refs/remotes/origin/dev",
            self.commit,
        )
        with patch.object(module, "_push_once", wraps=module._push_once) as push:
            self._assert_failure("REMOTE_HEAD_MISMATCH", self._call)
            push.assert_not_called()
        self.assertEqual(self._remote_head(), self.expected_remote)

    def test_remote_race_fails_closed_without_force(self) -> None:
        real_push = module._push_once
        raced: list[str] = []

        def racing_push(*args, **kwargs):
            raced.append(self._advance_remote("race\n"))
            return real_push(*args, **kwargs)

        with patch.object(module, "_push_once", side_effect=racing_push):
            self._assert_failure("REMOTE_HEAD_MISMATCH", self._call)
        self.assertEqual(self._remote_head(), raced[0])

    def test_rejected_remote_push_is_bounded_push_failed(self) -> None:
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        self._assert_failure("PUSH_FAILED", self._call)
        self.assertEqual(self._remote_head(), self.expected_remote)

    def test_transport_ambiguity_is_recovered_when_remote_equals_commit(self) -> None:
        real_push = module._push_once

        def ambiguous_push(*args, **kwargs):
            real_push(*args, **kwargs)
            raise RepoFastForwardFailure(
                "DEADLINE_EXCEEDED",
                "simulated transport ambiguity",
                retryable=True,
            )

        with patch.object(module, "_push_once", side_effect=ambiguous_push):
            result = self._call()
        self.assertEqual(result.status, "published")
        self.assertTrue(result.push_attempted)
        self.assertEqual(result.remote_head_after, self.commit)

    def test_unobservable_post_push_state_is_publication_ambiguous(self) -> None:
        real_run = module._run_git
        observations = 0

        def unstable_run(repo, args, **kwargs):
            nonlocal observations
            if args and args[0] == "ls-remote":
                observations += 1
                if observations == 3:
                    raise RepoFastForwardFailure(
                        "DEADLINE_EXCEEDED",
                        "simulated post observation failure",
                        retryable=True,
                    )
            return real_run(repo, args, **kwargs)

        with patch.object(module, "_run_git", side_effect=unstable_run):
            self._assert_failure("PUBLICATION_AMBIGUOUS", self._call)
        self.assertEqual(self._remote_head(), self.commit)

    def test_dirty_tracked_rejects_before_network(self) -> None:
        (self.local / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure("DIRTY_WORKTREE", self._call)
            observe.assert_not_called()

    def test_staged_rejects_before_network(self) -> None:
        (self.local / "tracked.txt").write_text("staged\n", encoding="utf-8")
        _git(self.local, "add", "tracked.txt")
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure("DIRTY_WORKTREE", self._call)
            observe.assert_not_called()

    def test_untracked_rejects_before_network(self) -> None:
        (self.local / "untracked.txt").write_text("u\n", encoding="utf-8")
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure("DIRTY_WORKTREE", self._call)
            observe.assert_not_called()

    def test_conflicted_index_rejects_before_network(self) -> None:
        _git(self.local, "branch", "other", self.expected_remote)
        _git(self.local, "checkout", "-q", "other")
        (self.local / "tracked.txt").write_text("other\n", encoding="utf-8")
        _git(self.local, "commit", "-qam", "other")
        _git(self.local, "checkout", "-q", "dev")
        _git(self.local, "merge", "other", check=False)
        git_dir = Path(_git(self.local, "rev-parse", "--absolute-git-dir"))
        (git_dir / "MERGE_HEAD").unlink(missing_ok=True)
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure("DIRTY_WORKTREE", self._call)
            observe.assert_not_called()

    def test_in_progress_state_rejects_before_network(self) -> None:
        git_dir = Path(_git(self.local, "rev-parse", "--absolute-git-dir"))
        (git_dir / "MERGE_HEAD").write_text(self.expected_remote + "\n")
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure("OPERATION_IN_PROGRESS", self._call)
            observe.assert_not_called()

    def test_subdirectory_detached_branch_upstream_and_head_mismatch(self) -> None:
        nested = self.local / "nested"
        nested.mkdir()
        self._assert_failure(
            "NOT_REPOSITORY_ROOT",
            lambda: publish_repository(
                str(nested), "dev", self.expected_remote, self.commit
            ),
        )

        _git(self.local, "checkout", "-q", "--detach", self.commit)
        self._assert_failure("DETACHED_HEAD", self._call)
        _git(self.local, "checkout", "-q", "dev")

        self._assert_failure(
            "BRANCH_MISMATCH",
            lambda: publish_repository(
                str(self.local), "main", self.expected_remote, self.commit
            ),
        )

        _git(self.local, "branch", "--unset-upstream")
        self._assert_failure("UPSTREAM_MISMATCH", self._call)
        _git(self.local, "branch", "--set-upstream-to=origin/dev", "dev")

        self._assert_failure(
            "LOCAL_HEAD_MISMATCH",
            lambda: publish_repository(
                str(self.local), "dev", self.expected_remote, self.expected_remote
            ),
        )

    def test_two_commit_range_is_rejected_before_network(self) -> None:
        (self.local / "second.txt").write_text("two\n", encoding="utf-8")
        _git(self.local, "add", "second.txt")
        _git(self.local, "commit", "-q", "-m", "second")
        second = _git(self.local, "rev-parse", "HEAD")
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure(
                "PUBLICATION_LINEAGE_MISMATCH",
                lambda: publish_repository(
                    str(self.local), "dev", self.expected_remote, second
                ),
            )
            observe.assert_not_called()

    def test_merge_commit_is_rejected_before_network(self) -> None:
        _git(self.local, "branch", "side", self.expected_remote)
        _git(self.local, "checkout", "-q", "side")
        (self.local / "side.txt").write_text("side\n", encoding="utf-8")
        _git(self.local, "add", "side.txt")
        _git(self.local, "commit", "-q", "-m", "side")
        _git(self.local, "checkout", "-q", "dev")
        _git(self.local, "merge", "-q", "--no-ff", "side", "-m", "merge")
        merge = _git(self.local, "rev-parse", "HEAD")
        with patch.object(module, "_observe_remote_head") as observe:
            self._assert_failure(
                "PUBLICATION_LINEAGE_MISMATCH",
                lambda: publish_repository(
                    str(self.local), "dev", self.expected_remote, merge
                ),
            )
            observe.assert_not_called()

    def test_root_commit_lineage_is_rejected(self) -> None:
        root = _git(self.local, "rev-list", "--max-parents=0", "HEAD")
        self._assert_failure(
            "PUBLICATION_LINEAGE_MISMATCH",
            lambda: module._require_direct_child(
                self.local,
                self.expected_remote,
                root,
                time.monotonic() + 5,
            ),
        )

    def test_local_state_change_immediately_before_push_fails_closed(self) -> None:
        real_check = module._require_unchanged_local_state

        def racing_check(repo, initial, deadline):
            real_check(repo, initial, deadline)
            (self.local / "raced.txt").write_text("race\n", encoding="utf-8")
            module._require_unchanged_local_state(repo, initial, deadline)

        with patch.object(
            module,
            "_require_unchanged_local_state",
            side_effect=racing_check,
        ):
            self._assert_failure("LOCAL_STATE_CHANGED", self._call)
        self.assertEqual(self._remote_head(), self.expected_remote)

    def test_local_state_change_after_final_remote_observation_fails_closed(self) -> None:
        real_observe = module._observe_remote_head
        observations = 0

        def racing_observe(repo, branch, deadline, *, after_push):
            nonlocal observations
            result = real_observe(
                repo,
                branch,
                deadline,
                after_push=after_push,
            )
            observations += 1
            if observations == 2:
                (self.local / "after-final-observe.txt").write_text(
                    "race\n",
                    encoding="utf-8",
                )
            return result

        with patch.object(
            module,
            "_observe_remote_head",
            side_effect=racing_observe,
        ):
            with patch.object(module, "_push_once", wraps=module._push_once) as push:
                self._assert_failure("LOCAL_STATE_CHANGED", self._call)
                push.assert_not_called()
        self.assertEqual(self._remote_head(), self.expected_remote)

    def test_push_argv_is_fixed_single_ref_exact_lease_and_hooks_disabled(self) -> None:
        real_run = module._run_git
        calls: list[tuple[list[str], dict[str, object]]] = []

        def recording_run(repo, args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            return real_run(repo, args, **kwargs)

        with patch.object(module, "_run_git", side_effect=recording_run):
            self._call()

        push_calls = [
            (args, kwargs)
            for args, kwargs in calls
            if "push" in args
        ]
        self.assertEqual(len(push_calls), 1)
        push_args, push_kwargs = push_calls[0]
        lease = (
            "--force-with-lease="
            f"refs/heads/dev:{self.expected_remote}"
        )
        self.assertEqual(push_args[0:3], ["-c", "push.followTags=false", "push"])
        self.assertIn("--no-verify", push_args)
        self.assertIn("--recurse-submodules=no", push_args)
        self.assertIn(lease, push_args)
        self.assertIn("origin", push_args)
        self.assertEqual(
            push_args[-1],
            f"{self.commit}:refs/heads/dev",
        )
        self.assertTrue(push_kwargs["disable_hooks"])
        forbidden = {
            "--force",
            "-f",
            "--force-with-lease",
            "--mirror",
            "--all",
            "--tags",
            "--follow-tags",
            "--delete",
        }
        self.assertFalse(forbidden.intersection(push_args), push_args)
        self.assertFalse(any(arg.startswith("+") for arg in push_args))
        refspecs = [arg for arg in push_args if ":refs/" in arg and not arg.startswith("--")]
        self.assertEqual(refspecs, [f"{self.commit}:refs/heads/dev"])
        self.assertFalse(any(args and args[0] == "fetch" for args, _ in calls))

    def test_disposable_native_mutation_chain_publishes_exact_cas(self) -> None:
        chain = self.root / "chain"
        _git(
            self.root,
            "clone",
            "-q",
            "-b",
            "dev",
            str(self.remote),
            str(chain),
        )
        parent = _git(chain, "rev-parse", "HEAD")
        target = chain / "tracked.txt"
        before_sha = hashlib.sha256(target.read_bytes()).hexdigest()

        written = write_file(
            str(chain),
            "tracked.txt",
            "replace",
            "native chain\n",
            before_sha,
        )
        staged = stage_repository(
            str(chain),
            "dev",
            parent,
            [
                RepoStageItem(
                    path="tracked.txt",
                    operation="present",
                    expected_sha256=written.sha256_after,
                )
            ],
        )
        diff = diff_repository(str(chain), "staged")
        self.assertEqual(diff.diff_receipt, staged.staged_diff_receipt)

        committed = commit_repository(
            str(chain),
            "dev",
            parent,
            diff.diff_receipt,
            "native mutation chain",
        )
        self.assertEqual(committed.parent_sha, parent)
        self.assertEqual(_git(chain, "rev-parse", "HEAD"), committed.commit_sha)

        published = publish_repository(
            str(chain),
            "dev",
            parent,
            committed.commit_sha,
        )
        self.assertEqual(published.status, "published")
        self.assertEqual(published.remote_head_after, committed.commit_sha)
        remote_head = _git(
            chain,
            "ls-remote",
            "--exit-code",
            "origin",
            "refs/heads/dev",
        ).split()[0]
        self.assertEqual(remote_head, committed.commit_sha)

    def test_push_deadline_preserves_post_observation_budget(self) -> None:
        with patch.object(module.time, "monotonic", return_value=10.0):
            self.assertEqual(module._push_deadline(20.0), 15.0)
            self._assert_failure(
                "DEADLINE_EXCEEDED",
                lambda: module._push_deadline(14.0),
            )


if __name__ == "__main__":
    unittest.main()
