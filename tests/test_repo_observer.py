from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_runtime.repo_observer as repo_observer_module
from agent_runtime.repo_observer import (
    CALL_DEADLINE_SECONDS,
    MAX_PATHS_LIMIT,
    RepoObserverFailure,
    _normalize_status,
    observe_repository,
)

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
GIT = "/usr/bin/git"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        [GIT, *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return completed.stdout.strip()


def _git_fail(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RepoObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        BUILD.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix=".task-0085-test-", dir=BUILD)
        self.addCleanup(self._tmp.cleanup)
        self.task_root = Path(self._tmp.name)
        self.repo = self.task_root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "Task 0085")
        _git(self.repo, "config", "user.email", "task0085@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        (self.repo / "old.txt").write_text("old\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt", "old.txt")
        _git(self.repo, "commit", "-q", "-m", "initial")
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.task_root)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_clean_repository_reports_safety_local_branch_and_no_tracking(self) -> None:
        result = observe_repository(str(self.repo), max_paths=20)

        self.assertEqual(result.repository.root, str(self.repo.resolve()))
        self.assertEqual(result.repository.cwd, str(self.repo.resolve()))
        self.assertFalse(result.repository.bare)
        self.assertFalse(result.repository.shallow)
        self.assertTrue(result.repository.inside_workspace_root)
        self.assertTrue(result.repository.cwd_inside_repo)
        self.assertTrue(result.repository.cwd_is_repo_root)
        self.assertEqual(result.branch.name, "main")
        self.assertFalse(result.branch.detached)
        self.assertEqual(result.branch.head_sha, _git(self.repo, "rev-parse", "HEAD"))
        self.assertIsNone(result.tracking.upstream)
        self.assertIsNone(result.tracking.tracking_sha)
        self.assertFalse(result.tracking.tracking_known)
        self.assertIsNone(result.tracking.ahead)
        self.assertIsNone(result.tracking.behind)
        self.assertEqual(result.changes, [])
        self.assertEqual(result.truncation.total_changes, 0)
        self.assertTrue(result.truncation.total_changes_exact)
        self.assertFalse(result.truncation.changes_truncated)
        self.assertFalse(result.observation.fetched)
        self.assertFalse(result.observation.network_used)
        self.assertEqual(result.observation.deadline_seconds, CALL_DEADLINE_SECONDS)
        self.assertEqual(result.worktrees.outside_workspace_count, 0)
        self.assertEqual(result.worktrees.total_count, 1)
        self.assertTrue(result.worktrees.total_exact)
        self.assertEqual(result.worktrees.entries[0].path, str(self.repo.resolve()))

    def test_subdirectory_cwd_is_supported_and_safety_flags_are_truthful(self) -> None:
        subdir = self.repo / "nested" / "dir"
        subdir.mkdir(parents=True)

        result = observe_repository(str(subdir), max_paths=20)

        self.assertEqual(result.repository.root, str(self.repo.resolve()))
        self.assertEqual(result.repository.cwd, str(subdir.resolve()))
        self.assertTrue(result.repository.inside_workspace_root)
        self.assertTrue(result.repository.cwd_inside_repo)
        self.assertFalse(result.repository.cwd_is_repo_root)

    def test_canonical_changes_cover_index_worktree_untracked_deleted_and_rename(self) -> None:
        (self.repo / "deleted.txt").write_text("delete me\n", encoding="utf-8")
        _git(self.repo, "add", "deleted.txt")
        _git(self.repo, "commit", "-q", "-m", "add deleted fixture")

        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        (self.repo / "tracked.txt").write_text("three\n", encoding="utf-8")
        _git(self.repo, "mv", "old.txt", "new.txt")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "untracked.txt").write_text("u\n", encoding="utf-8")

        result = observe_repository(str(self.repo), max_paths=20)
        by_path = {change.path: change for change in result.changes}

        both = by_path["tracked.txt"]
        self.assertEqual((both.index_status, both.worktree_status), ("M", "M"))
        self.assertTrue(both.tracked)
        self.assertTrue(both.staged)
        self.assertFalse(both.conflicted)

        renamed = by_path["new.txt"]
        self.assertEqual(renamed.index_status, "R")
        self.assertEqual(renamed.original_path, "old.txt")
        self.assertTrue(renamed.staged)

        deleted = by_path["deleted.txt"]
        self.assertEqual(deleted.worktree_status, "D")
        self.assertTrue(deleted.tracked)
        self.assertFalse(deleted.staged)

        untracked = by_path["untracked.txt"]
        self.assertFalse(untracked.tracked)
        self.assertFalse(untracked.staged)
        self.assertEqual(untracked.worktree_status, "?")

    def test_max_paths_bounds_retained_records_but_exact_total_is_preserved_when_enumeration_completes(self) -> None:
        for index in range(8):
            (self.repo / f"u-{index}.txt").write_text("x\n", encoding="utf-8")

        result = observe_repository(str(self.repo), max_paths=3)

        self.assertEqual(len(result.changes), 3)
        self.assertTrue(result.truncation.changes_truncated)
        self.assertEqual(result.truncation.total_changes, 8)
        self.assertTrue(result.truncation.total_changes_exact)

    def test_large_status_output_is_bounded_and_does_not_claim_exact_total_after_byte_truncation(self) -> None:
        for index in range(80):
            name = f"untracked-{index:03d}-" + ("x" * 40) + ".txt"
            (self.repo / name).write_text("x\n", encoding="utf-8")

        with patch.object(repo_observer_module, "_STATUS_MAX_BYTES", 256):
            result = observe_repository(str(self.repo), max_paths=5)

        self.assertLessEqual(len(result.changes), 5)
        self.assertTrue(result.truncation.changes_truncated)
        self.assertIsNone(result.truncation.total_changes)
        self.assertFalse(result.truncation.total_changes_exact)

    def test_binary_numstat_is_recognized_and_line_counts_become_explicitly_unavailable(self) -> None:
        binary = self.repo / "binary.bin"
        binary.write_bytes(b"\x00one")
        _git(self.repo, "add", "binary.bin")
        _git(self.repo, "commit", "-q", "-m", "binary fixture")
        binary.write_bytes(b"\x00two")

        result = observe_repository(str(self.repo), max_paths=20)

        self.assertIsNone(result.diff_summary.additions)
        self.assertIsNone(result.diff_summary.deletions)
        self.assertFalse(result.diff_summary.exact)

    def test_unknown_porcelain_status_code_fails_closed(self) -> None:
        with self.assertRaises(RepoObserverFailure) as raised:
            _normalize_status("Z")
        self.assertEqual(raised.exception.code, "INTERNAL_ERROR")

    def test_local_tracking_known_ahead_behind_and_missing_ref_are_distinguished_without_fetch(self) -> None:
        _git(self.repo, "branch", "upstream")
        _git(self.repo, "branch", "--set-upstream-to=upstream", "main")

        known = observe_repository(str(self.repo), max_paths=20)
        self.assertEqual(known.tracking.upstream, "upstream")
        self.assertTrue(known.tracking.tracking_known)
        self.assertEqual(known.tracking.tracking_sha, _git(self.repo, "rev-parse", "upstream"))
        self.assertEqual((known.tracking.ahead, known.tracking.behind), (0, 0))

        (self.repo / "main-only.txt").write_text("main\n", encoding="utf-8")
        _git(self.repo, "add", "main-only.txt")
        _git(self.repo, "commit", "-q", "-m", "main ahead")
        ahead = observe_repository(str(self.repo), max_paths=20)
        self.assertEqual((ahead.tracking.ahead, ahead.tracking.behind), (1, 0))

        _git(self.repo, "checkout", "-q", "upstream")
        (self.repo / "upstream-only.txt").write_text("upstream\n", encoding="utf-8")
        _git(self.repo, "add", "upstream-only.txt")
        _git(self.repo, "commit", "-q", "-m", "upstream ahead")
        _git(self.repo, "checkout", "-q", "main")
        diverged = observe_repository(str(self.repo), max_paths=20)
        self.assertEqual((diverged.tracking.ahead, diverged.tracking.behind), (1, 1))
        self.assertEqual(diverged.tracking.tracking_sha, _git(self.repo, "rev-parse", "upstream"))

        _git(self.repo, "config", "branch.main.remote", ".")
        _git(self.repo, "config", "branch.main.merge", "refs/heads/missing")
        missing = observe_repository(str(self.repo), max_paths=20)
        self.assertEqual(missing.tracking.upstream, "missing")
        self.assertFalse(missing.tracking.tracking_known)
        self.assertIsNone(missing.tracking.tracking_sha)
        self.assertIsNone(missing.tracking.ahead)
        self.assertIsNone(missing.tracking.behind)
        self.assertFalse(missing.observation.fetched)
        self.assertFalse(missing.observation.network_used)

    def test_hostile_helpers_no_network_verbs_and_no_repository_mutation(self) -> None:
        marker = self.repo / "helper-ran"
        helper = self.repo / "helper.sh"
        helper.write_text("#!/bin/sh\nprintf x >> \"$1\"\ncat\n", encoding="utf-8")
        helper.chmod(0o755)

        (self.repo / ".gitattributes").write_text("tracked.txt diff=hostile\n", encoding="utf-8")
        _git(self.repo, "add", ".gitattributes")
        _git(self.repo, "commit", "-q", "-m", "hostile attributes fixture")
        _git(self.repo, "config", "core.fsmonitor", f"{helper} {marker}")
        _git(self.repo, "config", "core.pager", f"{helper} {marker}")
        _git(self.repo, "config", "diff.external", f"{helper} {marker}")
        _git(self.repo, "config", "diff.hostile.textconv", f"{helper} {marker}")
        marker.unlink(missing_ok=True)
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

        index = self.repo / ".git" / "index"
        head = self.repo / ".git" / "refs" / "heads" / "main"
        before = {
            "index_sha": _sha(index),
            "index_mtime": index.stat().st_mtime_ns,
            "head_sha": _sha(head),
            "head_mtime": head.stat().st_mtime_ns,
            "worktree_sha": _sha(self.repo / "tracked.txt"),
        }

        real_popen = subprocess.Popen
        calls: list[tuple[list[str], dict[str, object]]] = []

        def recording_popen(argv, *args, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return real_popen(argv, *args, **kwargs)

        with patch("agent_runtime.repo_observer.subprocess.Popen", side_effect=recording_popen):
            result = observe_repository(str(self.repo), max_paths=20)

        self.assertFalse(marker.exists())
        self.assertFalse(result.observation.network_used)
        self.assertFalse(result.observation.fetched)
        self.assertEqual(_sha(index), before["index_sha"])
        self.assertEqual(index.stat().st_mtime_ns, before["index_mtime"])
        self.assertEqual(_sha(head), before["head_sha"])
        self.assertEqual(head.stat().st_mtime_ns, before["head_mtime"])
        self.assertEqual(_sha(self.repo / "tracked.txt"), before["worktree_sha"])

        self.assertGreaterEqual(len(calls), 7)
        network_verbs = {"fetch", "pull", "push", "clone", "ls-remote", "archive", "submodule"}
        for argv, kwargs in calls:
            self.assertEqual(argv[0], GIT)
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            env = kwargs["env"]
            self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["GIT_PAGER"], "cat")
            self.assertIn("core.fsmonitor=false", argv)
            self.assertFalse(set(argv) & network_verbs)
            if "diff" in argv:
                self.assertIn("--no-ext-diff", argv)
                self.assertIn("--no-textconv", argv)

    def test_outside_workspace_and_non_repository_are_typed_failures(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.repo)}):
            with self.assertRaises(RepoObserverFailure) as outside:
                observe_repository(str(self.task_root), max_paths=20)
        self.assertEqual(outside.exception.code, "OUTSIDE_WORKSPACE")
        self.assertFalse(outside.exception.retryable)

        plain = self.task_root / "plain"
        plain.mkdir()
        (plain / ".git").write_text("gitdir: ./missing\n", encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(plain)}):
            with self.assertRaises(RepoObserverFailure) as not_repo:
                observe_repository(str(plain), max_paths=20)
        self.assertEqual(not_repo.exception.code, "NOT_GIT_REPOSITORY")

    def test_extra_worktree_outside_narrow_workspace_is_redacted_without_path_inspection(self) -> None:
        outside = self.task_root / "outside-worktree"
        _git(self.repo, "branch", "outside-branch")
        _git(self.repo, "worktree", "add", "-q", str(outside), "outside-branch")
        self.addCleanup(
            lambda: subprocess.run(
                [GIT, "-C", str(self.repo), "worktree", "remove", "--force", str(outside)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

        original_resolve = Path.resolve
        original_stat = Path.stat
        inspected: list[tuple[str, str]] = []

        def tracking_resolve(path_self: Path, *args, **kwargs):
            inspected.append(("resolve", str(path_self)))
            return original_resolve(path_self, *args, **kwargs)

        def tracking_stat(path_self: Path, *args, **kwargs):
            inspected.append(("stat", str(path_self)))
            return original_stat(path_self, *args, **kwargs)

        with (
            patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.repo)}),
            patch.object(Path, "resolve", tracking_resolve),
            patch.object(Path, "stat", tracking_stat),
        ):
            result = observe_repository(str(self.repo), max_paths=20)

        serialized = result.model_dump_json()
        self.assertEqual(result.worktrees.outside_workspace_count, 1)
        self.assertEqual(result.worktrees.total_count, 2)
        self.assertTrue(result.worktrees.total_exact)
        self.assertNotIn(str(outside), serialized)
        self.assertFalse(
            any(path == str(outside) or path.startswith(str(outside) + os.sep) for _op, path in inspected),
            inspected,
        )

    def test_detached_head_and_merge_conflict_are_represented(self) -> None:
        _git(self.repo, "checkout", "-q", "-b", "other")
        (self.repo / "tracked.txt").write_text("other\n", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "other")
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "tracked.txt").write_text("main\n", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "main")
        merge = _git_fail(self.repo, "merge", "other")
        self.assertNotEqual(merge.returncode, 0)

        conflicted = observe_repository(str(self.repo), max_paths=20)
        conflict = next(change for change in conflicted.changes if change.path == "tracked.txt")
        self.assertTrue(conflict.conflicted)
        self.assertTrue(conflicted.operation_state.merge)

        _git(self.repo, "merge", "--abort")
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "--detach", head)
        detached = observe_repository(str(self.repo), max_paths=20)
        self.assertTrue(detached.branch.detached)
        self.assertIsNone(detached.branch.name)

    def test_rebase_cherry_pick_and_bisect_states_are_represented(self) -> None:
        initial = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "-q", "-b", "topic")
        (self.repo / "tracked.txt").write_text("topic\n", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "topic")
        topic_sha = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "tracked.txt").write_text("main\n", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "main")
        cherry = _git_fail(self.repo, "cherry-pick", topic_sha)
        self.assertNotEqual(cherry.returncode, 0)
        cherry_state = observe_repository(str(self.repo), max_paths=20)
        self.assertTrue(cherry_state.operation_state.cherry_pick)
        _git(self.repo, "cherry-pick", "--abort")

        _git(self.repo, "checkout", "-q", "-b", "rebase-topic", initial)
        (self.repo / "tracked.txt").write_text("rebase\n", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "rebase topic")
        rebase = _git_fail(self.repo, "rebase", "main")
        self.assertNotEqual(rebase.returncode, 0)
        rebase_state = observe_repository(str(self.repo), max_paths=20)
        self.assertTrue(rebase_state.operation_state.rebase)
        _git(self.repo, "rebase", "--abort")

        _git(self.repo, "checkout", "-q", "main")
        for index in range(2):
            (self.repo / f"bisect-{index}.txt").write_text(f"{index}\n", encoding="utf-8")
            _git(self.repo, "add", f"bisect-{index}.txt")
            _git(self.repo, "commit", "-q", "-m", f"bisect {index}")
        _git(self.repo, "bisect", "start", "HEAD", initial)
        bisect_state = observe_repository(str(self.repo), max_paths=20)
        self.assertTrue(bisect_state.operation_state.bisect)
        _git(self.repo, "bisect", "reset")

    def test_shallow_flag_is_represented_and_bare_repository_is_typed_rejection(self) -> None:
        head = _git(self.repo, "rev-parse", "HEAD")
        shallow_file = self.repo / ".git" / "shallow"
        shallow_file.write_text(head + "\n", encoding="ascii")
        shallow = observe_repository(str(self.repo), max_paths=20)
        self.assertTrue(shallow.repository.shallow)
        shallow_file.unlink()

        bare = self.task_root / "bare.git"
        _git(self.task_root, "init", "-q", "--bare", str(bare))
        with self.assertRaises(RepoObserverFailure) as rejected:
            observe_repository(str(bare), max_paths=20)
        self.assertEqual(rejected.exception.code, "INVALID_ARGUMENT")

    def test_shared_monotonic_deadline_passes_only_nonincreasing_remaining_time_to_every_git_operation(self) -> None:
        real_run = repo_observer_module._run_git_records
        remaining_values: list[float] = []

        def recording_run(*args, **kwargs):
            remaining_values.append(kwargs["remaining_time"])
            return real_run(*args, **kwargs)

        with patch.object(repo_observer_module, "_run_git_records", side_effect=recording_run):
            observe_repository(str(self.repo), max_paths=20)

        self.assertGreaterEqual(len(remaining_values), 7)
        self.assertTrue(all(0 < value <= CALL_DEADLINE_SECONDS for value in remaining_values))
        self.assertTrue(
            all(later <= earlier for earlier, later in zip(remaining_values, remaining_values[1:])),
            remaining_values,
        )

        with patch.object(repo_observer_module, "CALL_DEADLINE_SECONDS", 0.0):
            with self.assertRaises(RepoObserverFailure) as expired:
                observe_repository(str(self.repo), max_paths=20)
        self.assertEqual(expired.exception.code, "DEADLINE_EXCEEDED")
        self.assertTrue(expired.exception.retryable)

    def test_max_paths_argument_is_closed_and_bounded(self) -> None:
        self.assertGreaterEqual(MAX_PATHS_LIMIT, 20)
        for invalid in (0, MAX_PATHS_LIMIT + 1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RepoObserverFailure) as raised:
                    observe_repository(str(self.repo), max_paths=invalid)
                self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
