from __future__ import annotations

import fcntl
import inspect
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_runtime.runner as runner_module
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

    def test_config_generation_change_requires_restart_before_verify(self) -> None:
        fx = RuntimeFixture(self)
        changed = fx.config.read_text(encoding="utf-8").replace(
            'verify_argv = ["./verify"]',
            'verify_argv = ["bash", "-lc", "echo generation-drift > ignored.tmp; exit 0"]',
        )
        fx.config.write_text(changed, encoding="utf-8")
        launch = fx.runtime.run_verify("example-main")
        terminal = fx.wait_terminal() if launch.get("accepted") else None
        print(
            "AR01_CONFIG_DIAGNOSTIC",
            {
                "accepted": launch.get("accepted"),
                "terminal": terminal.get("status") if terminal else None,
                "changed_verifier_ran": (fx.checkout / "ignored.tmp").exists(),
            },
        )
        self.assertFalse(launch["accepted"])
        self.assertIn("restart", launch["error"].lower())
        self.assertFalse((fx.checkout / "ignored.tmp").exists())

    def test_runtime_source_generation_change_requires_restart_before_verify(self) -> None:
        fx = RuntimeFixture(self)
        original_read_bytes = Path.read_bytes
        runner_path = Path(runner_module.__file__).resolve()

        def drifted_read_bytes(path: Path) -> bytes:
            data = original_read_bytes(path)
            if path.resolve() == runner_path:
                return data + b"\n# simulated runtime generation drift\n"
            return data

        with mock.patch.object(Path, "read_bytes", new=drifted_read_bytes):
            launch = fx.runtime.run_verify("example-main")
        terminal = fx.wait_terminal() if launch.get("accepted") else None
        print(
            "AR01_RUNTIME_DIAGNOSTIC",
            {
                "accepted": launch.get("accepted"),
                "terminal": terminal.get("status") if terminal else None,
            },
        )
        self.assertFalse(launch["accepted"])
        self.assertIn("restart", launch["error"].lower())

    def test_worker_revalidates_expected_generation_before_verifier_execution(self) -> None:
        fx = RuntimeFixture(
            self,
            "#!/usr/bin/env bash\necho worker-ran > ignored.tmp\nexit 0\n",
        )
        params = inspect.signature(runner_module._run_verify_worker).parameters
        self.assertIn("expected_config_generation", params)
        self.assertIn("expected_runtime_generation", params)

        store = fx.runtime._store("example-main")
        lock_fd = runner_module._try_acquire_lock(store)
        self.assertIsNotNone(lock_fd)
        assert lock_fd is not None
        run_id = "worker-generation-mismatch"
        started_at = runner_module._utc_now()
        head = run(fx.checkout, "git", "rev-parse", "HEAD").stdout.strip()
        starting = runner_module._state_template(
            project="example-main",
            run_id=run_id,
            status="STARTING",
            head=head,
            started_at=started_at,
            timeout_seconds=fx.runtime._profile("example-main").timeout_seconds,
            launcher_pid=os.getpid(),
        )
        store.write_state(starting)
        store.prepare_log()
        starting["log_run_id"] = run_id
        store.write_state(starting)
        rc = runner_module._run_verify_worker(
            config_path=fx.config,
            project="example-main",
            lock_fd=lock_fd,
            run_id=run_id,
            expected_head=head,
            started_at=started_at,
            expected_config_generation="0" * 64,
            expected_runtime_generation="0" * 64,
        )
        print(
            "AR01_WORKER_DIAGNOSTIC",
            {"rc": rc, "changed_verifier_ran": (fx.checkout / "ignored.tmp").exists()},
        )
        self.assertEqual(rc, 70)
        self.assertFalse((fx.checkout / "ignored.tmp").exists())

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

    def test_stale_active_free_lock_rereads_concurrent_terminal_state(self) -> None:
        fx = RuntimeFixture(self)
        store = fx.runtime._store("example-main")
        store.ensure_directory()
        run_id = "race-terminal"
        head = run(fx.checkout, "git", "rev-parse", "HEAD").stdout.strip()
        active = {
            "version": 1,
            "project": "example-main",
            "run_id": run_id,
            "status": "RUNNING",
            "head": head,
            "launcher_pid": 101,
            "worker_pid": 102,
            "verify_pid": 103,
            "verify_pgid": 103,
            "timeout_seconds": 10,
            "started_at": "2026-08-27T00:00:00+00:00",
            "finished_at": None,
            "duration_seconds": None,
            "exit_code": None,
            "timed_out": False,
            "verification_ok": None,
            "failure_kind": None,
            "working_tree_after": None,
            "postconditions": {
                "same_head": None,
                "configured_branch": None,
                "clean": None,
                "in_sync": None,
                "remote_identity": None,
            },
            "log_run_id": run_id,
            "log_finalized": False,
        }
        terminal = dict(active)
        terminal.update(
            {
                "status": "PASS",
                "finished_at": "2026-08-27T00:00:01+00:00",
                "duration_seconds": 1.0,
                "exit_code": 0,
                "verification_ok": True,
                "working_tree_after": {
                    "ok": True,
                    "head": head,
                    "cached_remote_head": head,
                    "configured_branch": "main",
                    "current_branch": "main",
                    "clean": True,
                    "in_sync": True,
                    "remote_identity_ok": True,
                },
                "postconditions": {
                    "same_head": True,
                    "configured_branch": True,
                    "clean": True,
                    "in_sync": True,
                    "remote_identity": True,
                },
                "log_finalized": True,
            }
        )
        store.log_path.write_text("terminal diagnostics\n", encoding="utf-8")
        with (
            mock.patch.object(runner_module.StateStore, "read_state", side_effect=[(active, None), (terminal, None)]),
            mock.patch.object(runner_module, "_lock_busy", return_value=(False, None)),
        ):
            observed = fx.runtime.get_last_log("example-main")
        print(
            "AR04_DIAGNOSTIC",
            {
                "status": observed.get("status"),
                "recorded_status": observed.get("recorded_status"),
                "verification_ok": observed.get("verification_ok"),
            },
        )
        self.assertEqual(observed["status"], "PASS")
        self.assertTrue(observed["verification_ok"])
        self.assertIn("terminal diagnostics", observed["log_tail"])

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
