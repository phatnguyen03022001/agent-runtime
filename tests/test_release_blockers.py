from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agent_runtime.config import ConfigError, ProjectProfile, load_config
from agent_runtime.git_ops import GitError, sync_checkout
from agent_runtime.runner import AgentRuntime


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout)
    return result


def profile_text(project_id: str, checkout: Path, *, remote_url: str, state_dir: Path) -> str:
    return f'''version = 1\nstate_dir = {json.dumps(str(state_dir))}\n\n[projects.{project_id}]\nrepository = "owner/repo"\ncheckout = {json.dumps(str(checkout))}\nremote = "origin"\nexpected_remote_url = {json.dumps(remote_url)}\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = 5\ndisposable = true\n'''


class ReleaseBlockerTests(unittest.TestCase):
    def make_config(self, text: str) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "runtime.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_R1_public_errors_do_not_expose_checkout_or_state_paths(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        missing_checkout = root / "private" / "missing-checkout"
        state_dir = root / "private-state"
        config = self.make_config(
            profile_text(
                "example-main",
                missing_checkout,
                remote_url=str(root / "remote.git"),
                state_dir=state_dir,
            )
        )
        runtime = AgentRuntime(config)
        for operation in (runtime.get_head, runtime.sync, runtime.run_verify):
            with self.subTest(operation=operation.__name__):
                result = operation("example-main")
                rendered = repr(result)
                self.assertNotIn(str(missing_checkout), rendered)
                self.assertLess(len(rendered), 4096)

        state_file = root / "state-is-file"
        state_file.write_text("not a directory", encoding="utf-8")
        config2 = self.make_config(
            profile_text(
                "example-main",
                root / "checkout",
                remote_url=str(root / "remote.git"),
                state_dir=state_file,
            )
        )
        runtime2 = AgentRuntime(config2)
        result = runtime2.get_last_log("example-main")
        rendered = repr(result)
        self.assertNotIn(str(state_file), rendered)
        self.assertLess(len(rendered), 4096)

    def test_R2_sync_does_not_implicitly_create_missing_local_branch(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        remote = root / "remote.git"
        seed = root / "seed"
        checkout = root / "checkout"
        run(root, "git", "init", "--bare", str(remote))
        run(root, "git", "init", "-b", "main", str(seed))
        run(seed, "git", "config", "user.name", "Tests")
        run(seed, "git", "config", "user.email", "tests@example.invalid")
        (seed / "tracked.txt").write_text("remote main\n", encoding="utf-8")
        run(seed, "git", "add", "tracked.txt")
        run(seed, "git", "commit", "-m", "main")
        run(seed, "git", "remote", "add", "origin", str(remote))
        run(seed, "git", "push", "origin", "main")

        run(root, "git", "init", "-b", "other", str(checkout))
        run(checkout, "git", "config", "user.name", "Tests")
        run(checkout, "git", "config", "user.email", "tests@example.invalid")
        (checkout / "other.txt").write_text("other\n", encoding="utf-8")
        run(checkout, "git", "add", "other.txt")
        run(checkout, "git", "commit", "-m", "other")
        run(checkout, "git", "remote", "add", "origin", str(remote))

        profile = ProjectProfile(
            project_id="example-main",
            repository="owner/repo",
            checkout=checkout.resolve(),
            remote="origin",
            expected_remote_url=str(remote),
            branch="main",
            verify_argv=("./verify",),
            timeout_seconds=5,
            disposable=True,
        )
        with self.assertRaises(GitError):
            sync_checkout(profile)
        local_ref = run(checkout, "git", "show-ref", "--verify", "refs/heads/main", check=False)
        self.assertNotEqual(local_ref.returncode, 0)
        remote_ref = run(checkout, "git", "show-ref", "--verify", "refs/remotes/origin/main", check=False)
        self.assertEqual(remote_ref.returncode, 0)
        self.assertEqual(run(remote, "git", "rev-parse", "refs/heads/main").stdout.strip(), run(seed, "git", "rev-parse", "HEAD").stdout.strip())

    def test_R3_exit_zero_with_surviving_process_group_cannot_pass_and_is_cleaned(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        remote = root / "remote.git"
        checkout = root / "checkout"
        state_dir = root / "state"
        config = root / "runtime.toml"
        run(root, "git", "init", "--bare", str(remote))
        run(root, "git", "init", "-b", "main", str(checkout))
        run(checkout, "git", "config", "user.name", "Tests")
        run(checkout, "git", "config", "user.email", "tests@example.invalid")
        (checkout / ".gitignore").write_text("child.pid\n", encoding="utf-8")
        verify = checkout / "verify"
        verify.write_text("#!/usr/bin/env bash\nsleep 60 &\necho $! > child.pid\nexit 0\n", encoding="utf-8")
        verify.chmod(0o755)
        run(checkout, "git", "add", ".")
        run(checkout, "git", "commit", "-m", "base")
        run(checkout, "git", "remote", "add", "origin", str(remote))
        run(checkout, "git", "push", "-u", "origin", "main")
        config.write_text(profile_text("example-main", checkout, remote_url=str(remote), state_dir=state_dir), encoding="utf-8")
        runtime = AgentRuntime(config)
        child_pid: int | None = None
        try:
            launch = runtime.run_verify("example-main")
            self.assertTrue(launch["accepted"])
            end = time.monotonic() + 5
            result = {}
            while time.monotonic() < end:
                result = runtime.get_last_log("example-main")
                if result.get("status") in {"PASS", "FAIL", "TIMEOUT", "INTERRUPTED", "LAUNCH_FAILED"}:
                    break
                time.sleep(0.05)
            pid_path = checkout / "child.pid"
            if pid_path.exists():
                child_pid = int(pid_path.read_text().strip())
            self.assertNotEqual(result.get("status"), "PASS")
            self.assertFalse(result.get("verification_ok"))
            self.assertEqual(result.get("failure_kind"), "verifier_process_group_survived")
            self.assertIsNotNone(child_pid)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_R4_state_and_checkouts_must_not_overlap(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        def one(state_dir: Path, checkout: Path) -> str:
            return profile_text("one", checkout, remote_url=str(root / "remote.git"), state_dir=state_dir)

        overlap_cases = [
            (root / "same", root / "same"),
            (root / "checkout" / "state", root / "checkout"),
            (root / "state", root / "state" / "checkout"),
        ]
        for state_dir, checkout in overlap_cases:
            with self.subTest(state=state_dir, checkout=checkout), self.assertRaises(ConfigError):
                load_config(self.make_config(one(state_dir, checkout)))

        template = '''version = 1\nstate_dir = {state}\n\n[projects.one]\nrepository = "owner/one"\ncheckout = {one}\nremote = "origin"\nexpected_remote_url = "x"\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = 5\ndisposable = true\n\n[projects.two]\nrepository = "owner/two"\ncheckout = {two}\nremote = "origin"\nexpected_remote_url = "y"\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = 5\ndisposable = true\n'''
        pairs = [
            (root / "a", root / "a"),
            (root / "a", root / "a" / "nested"),
            (root / "a" / "nested", root / "a"),
        ]
        for first, second in pairs:
            text = template.format(state=json.dumps(str(root / "state")), one=json.dumps(str(first)), two=json.dumps(str(second)))
            with self.subTest(first=first, second=second), self.assertRaises(ConfigError):
                load_config(self.make_config(text))

        siblings = template.format(
            state=json.dumps(str(root / "state")),
            one=json.dumps(str(root / "checkout-a")),
            two=json.dumps(str(root / "checkout-b")),
        )
        config = load_config(self.make_config(siblings))
        self.assertEqual(set(config.projects), {"one", "two"})

    def test_R5_casefold_colliding_project_ids_are_rejected_but_lookup_stays_exact(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        text = f'''version = 1\nstate_dir = {json.dumps(str(root / "state"))}\n\n[projects.Foo]\nrepository = "owner/foo"\ncheckout = {json.dumps(str(root / "foo"))}\nremote = "origin"\nexpected_remote_url = "x"\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = 5\ndisposable = true\n\n[projects.foo]\nrepository = "owner/foo2"\ncheckout = {json.dumps(str(root / "foo2"))}\nremote = "origin"\nexpected_remote_url = "y"\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = 5\ndisposable = true\n'''
        with self.assertRaises(ConfigError):
            load_config(self.make_config(text))

        distinct = text.replace("[projects.Foo]", "[projects.foo-bar]")
        config = load_config(self.make_config(distinct))
        self.assertEqual(config.resolve("foo").project_id, "foo")
        self.assertEqual(config.resolve("foo-bar").project_id, "foo-bar")
        with self.assertRaises(ConfigError):
            config.resolve("FOO")

    def test_R6_invalid_project_input_is_not_echoed_and_responses_are_bounded(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = self.make_config(
            profile_text(
                "example-main",
                root / "checkout",
                remote_url=str(root / "remote.git"),
                state_dir=root / "state",
            )
        )
        runtime = AgentRuntime(config)
        bad = "../../tmp/secret;$(id);" + ("X" * 100_000)
        for operation in (runtime.get_head, runtime.sync, runtime.run_verify, runtime.get_last_log):
            with self.subTest(operation=operation.__name__):
                result = operation(bad)
                rendered = repr(result)
                self.assertNotIn(bad, rendered)
                self.assertNotIn("project", result)
                self.assertLess(len(rendered), 4096)
                self.assertEqual(result.get("error"), "Unknown project ID.")


if __name__ == "__main__":
    unittest.main()
