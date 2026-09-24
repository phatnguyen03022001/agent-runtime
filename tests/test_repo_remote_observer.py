from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server
import agent_runtime.repo_remote_observer as module
from agent_runtime.repo_remote_observer import (
    RepoRemoteObserverFailure,
    observe_remote_repository,
)

GIT = "/usr/bin/git"
REAL_POPEN = subprocess.Popen


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
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RepoRemoteObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0149-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.local = self.root / "local"

        _git(self.root, "init", "-q", "--bare", str(self.remote))
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "main")
        _git(self.seed, "config", "user.name", "Task 0149")
        _git(self.seed, "config", "user.email", "task0149@example.invalid")
        (self.seed / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.seed, "add", "tracked.txt")
        _git(self.seed, "commit", "-q", "-m", "initial")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-q", "-u", "origin", "main")

        _git(self.seed, "checkout", "-q", "-b", "feature")
        (self.seed / "feature.txt").write_text("feature\n", encoding="utf-8")
        _git(self.seed, "add", "feature.txt")
        _git(self.seed, "commit", "-q", "-m", "feature")
        _git(self.seed, "push", "-q", "-u", "origin", "feature")
        _git(self.seed, "checkout", "-q", "main")

        _git(self.root, "clone", "-q", "-b", "main", str(self.remote), str(self.local))
        old_remote = _git(self.local, "rev-parse", "refs/remotes/origin/main")
        (self.seed / "tracked.txt").write_text("remote-two\n", encoding="utf-8")
        _git(self.seed, "commit", "-qam", "advance main")
        _git(self.seed, "push", "-q", "origin", "main")
        self.fresh_remote = _git(self.seed, "rev-parse", "HEAD")
        self.assertNotEqual(old_remote, self.fresh_remote)

        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _snapshot(self) -> dict[str, object]:
        git_dir = Path(_git(self.local, "rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = self.local / git_dir
        return {
            "head": _git(self.local, "rev-parse", "HEAD"),
            "heads": _git(self.local, "show-ref", "--heads"),
            "remotes": _git(self.local, "show-ref", "--verify", "refs/remotes/origin/main", check=False),
            "fetch_head": _digest(git_dir / "FETCH_HEAD"),
            "index": _digest(git_dir / "index"),
            "config": _digest(git_dir / "config"),
            "status": subprocess.run(
                [GIT, "status", "--porcelain=v2", "-z"],
                cwd=str(self.local),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout,
        }

    def _assert_failure(self, code: str, callback) -> RepoRemoteObserverFailure:
        with self.assertRaises(RepoRemoteObserverFailure) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_fresh_exact_multi_branch_truth_ignores_stale_tracking_graph(self) -> None:
        before = self._snapshot()
        stale_tracking = _git(self.local, "rev-parse", "refs/remotes/origin/main")
        result = observe_remote_repository(str(self.local))
        after = self._snapshot()

        self.assertEqual(before, after)
        self.assertEqual(result.repository_root, str(self.local.resolve()))
        self.assertEqual(result.local_branch, "main")
        self.assertEqual(result.local_head, before["head"])
        self.assertEqual(result.remote, "origin")
        self.assertTrue(result.remote_branch_exists)
        self.assertEqual(result.remote_branch_head, self.fresh_remote)
        self.assertNotEqual(stale_tracking, result.remote_branch_head)
        self.assertEqual({item.name for item in result.remote_branches}, {"feature", "main"})
        self.assertEqual(result.branch_count, 2)
        self.assertFalse(result.fetched)
        self.assertTrue(result.network_used)
        self.assertFalse(result.local_refs_mutated)
        self.assertIsNone(result.ahead)
        self.assertIsNone(result.behind)

    def test_current_branch_absent_is_success(self) -> None:
        _git(self.local, "checkout", "-q", "-b", "local-only")
        result = observe_remote_repository(str(self.local))
        self.assertEqual(result.local_branch, "local-only")
        self.assertFalse(result.remote_branch_exists)
        self.assertIsNone(result.remote_branch_head)
        self.assertEqual(result.branch_count, 2)
        self.assertIsNone(result.ahead)
        self.assertIsNone(result.behind)

    def test_fixed_git_origin_prompt_disabled_and_shell_false(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def recording_popen(argv, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return REAL_POPEN(argv, **kwargs)

        with patch.object(module.subprocess, "Popen", side_effect=recording_popen):
            observe_remote_repository(str(self.local))

        self.assertTrue(calls)
        for argv, kwargs in calls:
            self.assertEqual(argv[0], GIT)
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        remote_calls = [argv for argv, _ in calls if "ls-remote" in argv]
        self.assertEqual(len(remote_calls), 1)
        remote_argv = remote_calls[0]
        pos = remote_argv.index("ls-remote")
        self.assertEqual(
            remote_argv[pos:],
            ["ls-remote", "--symref", "--branches", "origin"],
        )

    def test_malformed_and_duplicate_refs_fail_closed(self) -> None:
        for payload in (
            b"not-a-sha\trefs/heads/main\n",
            (b"a" * 40) + b"\trefs/tags/v1\n",
            (b"a" * 40) + b" refs/heads/main\n",
            (b"a" * 40) + b"\trefs/heads/main\n" + (b"a" * 40) + b"\trefs/heads/main\n",
        ):
            with self.subTest(payload=payload):
                self._assert_failure(
                    "TRANSIENT_FAILURE",
                    lambda payload=payload: module._parse_remote_refs(payload),
                )

    def test_branch_stdout_stderr_and_deadline_limits_fail_without_mutation(self) -> None:
        before = self._snapshot()
        with patch.object(module, "REMOTE_BRANCH_MAX", 1):
            self._assert_failure(
                "OUTPUT_LIMIT",
                lambda: observe_remote_repository(str(self.local)),
            )
        self.assertEqual(before, self._snapshot())

        with patch.object(module, "REMOTE_STDOUT_MAX_BYTES", 1):
            self._assert_failure(
                "OUTPUT_LIMIT",
                lambda: observe_remote_repository(str(self.local)),
            )
        self.assertEqual(before, self._snapshot())

        _git(self.local, "remote", "set-url", "origin", str(self.root / "missing.git"))
        stderr_before = self._snapshot()
        with patch.object(module, "STDERR_MAX_BYTES", 1):
            self._assert_failure(
                "OUTPUT_LIMIT",
                lambda: observe_remote_repository(str(self.local)),
            )
        self.assertEqual(stderr_before, self._snapshot())

        with patch.object(module, "CALL_DEADLINE_SECONDS", 0.0):
            self._assert_failure(
                "DEADLINE_EXCEEDED",
                lambda: observe_remote_repository(str(self.local)),
            )
        self.assertEqual(stderr_before, self._snapshot())

    def test_exact_root_and_attached_branch_are_required(self) -> None:
        child = self.local / "child"
        child.mkdir()
        self._assert_failure(
            "NOT_REPOSITORY_ROOT",
            lambda: observe_remote_repository(str(child)),
        )
        _git(self.local, "checkout", "-q", "--detach")
        self._assert_failure(
            "DETACHED_HEAD",
            lambda: observe_remote_repository(str(self.local)),
        )


class RepoRemoteObserverMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0149-mcp-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote = self.root / "origin.git"
        self.local = self.root / "local"
        _git(self.root, "init", "-q", "--bare", str(self.remote))
        self.local.mkdir()
        _git(self.local, "init", "-q", "-b", "main")
        _git(self.local, "config", "user.name", "Task 0149")
        _git(self.local, "config", "user.email", "task0149@example.invalid")
        (self.local / "a.txt").write_text("a\n", encoding="utf-8")
        _git(self.local, "add", "a.txt")
        _git(self.local, "commit", "-q", "-m", "initial")
        _git(self.local, "remote", "add", "origin", str(self.remote))
        _git(self.local, "push", "-q", "-u", "origin", "main")
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)

    async def test_public_schema_contract_and_success(self) -> None:
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(len(tools), 21)
        tool = tools["repo_remote_observer"]
        annotations = tool.annotations.model_dump(by_alias=True)
        self.assertEqual(
            (
                annotations["readOnlyHint"],
                annotations["destructiveHint"],
                annotations["idempotentHint"],
                annotations["openWorldHint"],
            ),
            (True, False, True, True),
        )
        self.assertEqual(set(tool.input_schema["properties"]), {"cwd"})
        self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertNotIn("remote", tool.input_schema["properties"])
        self.assertNotIn("url", tool.input_schema["properties"])

        result = await server.mcp.call_tool(
            "repo_remote_observer",
            {"cwd": str(self.local)},
        )
        self.assertFalse(result.is_error, result)
        Draft202012Validator(tool.output_schema).validate(result.structured_content)
        self.assertTrue(result.structured_content["network_used"])
        self.assertFalse(result.structured_content["fetched"])
        self.assertFalse(result.structured_content["local_refs_mutated"])
        self.assertIsNone(result.structured_content["ahead"])
        self.assertIsNone(result.structured_content["behind"])

    async def test_limit_failure_uses_structured_absent_effect_semantics(self) -> None:
        with patch.object(module, "REMOTE_BRANCH_MAX", 0):
            result = await server.mcp.call_tool(
                "repo_remote_observer",
                {"cwd": str(self.local)},
            )
        self.assertTrue(result.is_error, result)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "LIMIT_EXCEEDED")
        self.assertEqual(error["reason_code"], "OUTPUT_LIMIT")
        self.assertEqual(error["effect_state"], "absent")
        self.assertFalse(error["reconciliation_required"])


if __name__ == "__main__":
    unittest.main()
