from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agent_runtime.runner import AgentRuntime


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if check and result.returncode != 0:
        raise AssertionError(result.stdout)
    return result


class RuntimeFixture:
    def __init__(self, case: unittest.TestCase, verify_body: str = "#!/usr/bin/env bash\nexit 0\n", timeout: int = 10):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.checkout = root / "checkout"
        self.state_dir = root / "state"
        self.config = root / "runtime.toml"
        run(root, "git", "init", "--bare", str(self.remote))
        run(root, "git", "init", "-b", "main", str(self.checkout))
        run(self.checkout, "git", "config", "user.name", "Runtime Tests")
        run(self.checkout, "git", "config", "user.email", "runtime@example.invalid")
        (self.checkout / ".gitignore").write_text("ignored.tmp\nchild.pid\n", encoding="utf-8")
        (self.checkout / "tracked.txt").write_text("base\n", encoding="utf-8")
        verify = self.checkout / "verify"
        verify.write_text(verify_body, encoding="utf-8")
        verify.chmod(0o755)
        run(self.checkout, "git", "add", ".")
        run(self.checkout, "git", "commit", "-m", "base")
        run(self.checkout, "git", "remote", "add", "origin", str(self.remote))
        run(self.checkout, "git", "push", "-u", "origin", "main")
        self.config.write_text(
            f'''version = 1\nstate_dir = {json.dumps(str(self.state_dir))}\n\n[projects.example-main]\nrepository = "owner/repo"\ncheckout = {json.dumps(str(self.checkout))}\nremote = "origin"\nexpected_remote_url = {json.dumps(str(self.remote))}\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = {timeout}\ndisposable = true\n''',
            encoding="utf-8",
        )
        self.runtime = AgentRuntime(self.config)

    def wait_terminal(self, deadline: float = 8.0) -> dict:
        end = time.monotonic() + deadline
        last = {}
        while time.monotonic() < end:
            last = self.runtime.get_last_log("example-main")
            if last.get("status") in {"PASS", "FAIL", "TIMEOUT", "INTERRUPTED", "LAUNCH_FAILED"}:
                return last
            time.sleep(0.05)
        self.fail(f"verification did not finish: {last}")


class RunnerTests(unittest.TestCase):
    def test_get_head_is_read_only_bounded_and_does_not_leak_checkout_path(self) -> None:
        fx = RuntimeFixture(self)
        result = fx.runtime.get_head("example-main")
        self.assertEqual(result["project"], "example-main")
        self.assertEqual(result["repository"], "owner/repo")
        self.assertEqual(result["configured_branch"], "main")
        self.assertTrue(result["in_sync"])
        self.assertFalse(result["busy"])
        self.assertNotIn(str(fx.checkout), repr(result))

    def test_dirty_checkout_rejected(self) -> None:
        fx = RuntimeFixture(self)
        (fx.checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        result = fx.runtime.run_verify("example-main")
        self.assertFalse(result["accepted"])
        self.assertIn("dirty", result["error"].lower())

    def test_wrong_branch_rejected(self) -> None:
        fx = RuntimeFixture(self)
        run(fx.checkout, "git", "switch", "-c", "other")
        result = fx.runtime.run_verify("example-main")
        self.assertFalse(result["accepted"])
        self.assertIn("branch", result["error"].lower())

    def test_out_of_sync_cached_remote_rejected(self) -> None:
        fx = RuntimeFixture(self)
        (fx.checkout / "tracked.txt").write_text("ahead\n", encoding="utf-8")
        run(fx.checkout, "git", "add", "tracked.txt")
        run(fx.checkout, "git", "commit", "-m", "ahead")
        result = fx.runtime.run_verify("example-main")
        self.assertFalse(result["accepted"])
        self.assertIn("synchron", result["error"].lower())

    def test_remote_identity_mismatch_rejected_for_verify(self) -> None:
        fx = RuntimeFixture(self)
        run(fx.checkout, "git", "remote", "set-url", "origin", str(fx.remote) + "-wrong")
        result = fx.runtime.run_verify("example-main")
        self.assertFalse(result["accepted"])
        self.assertIn("remote", result["error"].lower())

    def test_verifier_exit_zero_with_valid_postconditions_passes(self) -> None:
        fx = RuntimeFixture(self)
        launch = fx.runtime.run_verify("example-main")
        self.assertTrue(launch["accepted"])
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["verification_ok"])
        self.assertTrue(result["postconditions"]["same_head"])
        self.assertTrue(result["postconditions"]["configured_branch"])
        self.assertTrue(result["postconditions"]["clean"])
        self.assertTrue(result["postconditions"]["in_sync"])

    def test_verifier_nonzero_fails(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\nexit 7\n")
        fx.runtime.run_verify("example-main")
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["verification_ok"])
        self.assertEqual(result["exit_code"], 7)

    def test_head_changed_during_verification_cannot_pass(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\necho changed >> tracked.txt\ngit add tracked.txt\ngit commit -m changed >/dev/null\nexit 0\n")
        fx.runtime.run_verify("example-main")
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["postconditions"]["same_head"])

    def test_branch_changed_during_verification_cannot_pass(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\ngit switch -c verifier-other >/dev/null\nexit 0\n")
        fx.runtime.run_verify("example-main")
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["postconditions"]["configured_branch"])

    def test_dirty_post_verification_checkout_cannot_pass(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\necho dirty >> tracked.txt\nexit 0\n")
        fx.runtime.run_verify("example-main")
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["postconditions"]["clean"])

    def test_timeout_terminates_verifier_process_group(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\n(sleep 60) &\necho $! > child.pid\nsleep 60\n", timeout=1)
        fx.runtime.run_verify("example-main")
        result = fx.wait_terminal(deadline=7.0)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertFalse(result["verification_ok"])
        pid = int((fx.checkout / "child.pid").read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_concurrent_mutating_operation_reports_busy(self) -> None:
        fx = RuntimeFixture(self)
        store = fx.runtime._store("example-main")
        store.ensure_directory()
        fd = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = fx.runtime.sync("example-main")
        self.assertFalse(result["ok"])
        self.assertTrue(result["busy"])

    def test_stale_running_state_is_fail_closed_and_recovered_under_lock(self) -> None:
        fx = RuntimeFixture(self)
        store = fx.runtime._store("example-main")
        store.ensure_directory()
        store.prepare_log()
        store.append_wrapper_log("partial diagnostics\n")
        store.write_state({
            "version": 1,
            "project": "example-main",
            "run_id": "stale",
            "status": "RUNNING",
            "head": run(fx.checkout, "git", "rev-parse", "HEAD").stdout.strip(),
            "verification_ok": None,
            "log_run_id": "stale",
            "log_finalized": False,
        })
        observed = fx.runtime.get_last_log("example-main")
        self.assertEqual(observed["status"], "INTERRUPTED")
        self.assertFalse(observed["verification_ok"])
        sync = fx.runtime.sync("example-main")
        self.assertTrue(sync["ok"])
        state, error = store.read_state()
        self.assertIsNone(error)
        self.assertEqual(state["status"], "INTERRUPTED")
        self.assertFalse(state["verification_ok"])
        self.assertIn("partial diagnostics", store.log_path.read_text())

    def test_corrupt_state_recovery_never_manufactures_pass(self) -> None:
        fx = RuntimeFixture(self)
        store = fx.runtime._store("example-main")
        store.ensure_directory()
        store.state_path.write_text("{corrupt", encoding="utf-8")
        result = fx.runtime.sync("example-main")
        self.assertTrue(result["ok"])
        state, error = store.read_state()
        self.assertIsNone(error)
        self.assertEqual(state["status"], "INTERRUPTED")
        self.assertFalse(state["verification_ok"])

    def test_sync_serializes_and_recovers_checkout(self) -> None:
        fx = RuntimeFixture(self)
        (fx.checkout / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        (fx.checkout / "untracked.txt").write_text("remove\n", encoding="utf-8")
        (fx.checkout / "ignored.tmp").write_text("keep\n", encoding="utf-8")
        result = fx.runtime.sync("example-main")
        self.assertTrue(result["ok"])
        self.assertEqual((fx.checkout / "tracked.txt").read_text(), "base\n")
        self.assertFalse((fx.checkout / "untracked.txt").exists())
        self.assertTrue((fx.checkout / "ignored.tmp").exists())

    def test_worker_launch_is_detached_and_lock_handoff_has_no_gap(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\nsleep 1\nexit 0\n", timeout=5)
        started = time.monotonic()
        launch = fx.runtime.run_verify("example-main")
        elapsed = time.monotonic() - started
        self.assertTrue(launch["accepted"])
        self.assertLess(elapsed, 0.8)
        busy = fx.runtime.sync("example-main")
        self.assertTrue(busy["busy"])
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
