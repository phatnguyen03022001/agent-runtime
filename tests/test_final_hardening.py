from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agent_runtime.runner import (
    AgentRuntime,
    _close_fd,
    _process_group_exists,
    _run_verify_worker,
    _state_template,
    _try_acquire_lock,
    _utc_now,
)
from agent_runtime.state import MAX_LOG_TAIL_BYTES, StateStore

PROJECT = "example-main"
PASS_RUN_ID = "0123456789abcdef0123456789abcdef"
LOG_LIMIT = 1024 * 1024


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


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_pid_gone(pid: int, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.02)
    return not pid_alive(pid)


class RuntimeFixture:
    def __init__(self, case: unittest.TestCase, verify_body: str, timeout: int = 5):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        self.remote = root / "remote.git"
        self.checkout = root / "checkout"
        self.state_dir = root / "state"
        self.config = root / "runtime.toml"
        run(root, "git", "init", "--bare", str(self.remote))
        run(root, "git", "init", "-b", "main", str(self.checkout))
        run(self.checkout, "git", "config", "user.name", "Hardening Tests")
        run(self.checkout, "git", "config", "user.email", "hardening@example.invalid")
        (self.checkout / ".gitignore").write_text(
            "leader.pid\nchild.pid\nenv.txt\n", encoding="utf-8"
        )
        (self.checkout / "tracked.txt").write_text("base\n", encoding="utf-8")
        verify = self.checkout / "verify"
        verify.write_text(verify_body, encoding="utf-8")
        verify.chmod(0o755)
        run(self.checkout, "git", "add", ".")
        run(self.checkout, "git", "commit", "-m", "base")
        run(self.checkout, "git", "remote", "add", "origin", str(self.remote))
        run(self.checkout, "git", "push", "-u", "origin", "main")
        self.config.write_text(
            f'''version = 1\nstate_dir = {json.dumps(str(self.state_dir))}\n\n[projects.{PROJECT}]\nrepository = "owner/repo"\ncheckout = {json.dumps(str(self.checkout))}\nremote = "origin"\nexpected_remote_url = {json.dumps(str(self.remote))}\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = {timeout}\ndisposable = true\n''',
            encoding="utf-8",
        )
        self.runtime = AgentRuntime(self.config)

    @property
    def store(self) -> StateStore:
        return self.runtime._store(PROJECT)

    def head(self) -> str:
        return run(self.checkout, "git", "rev-parse", "HEAD").stdout.strip()

    def wait_terminal(self, timeout: float = 10.0) -> dict:
        end = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < end:
            last = self.runtime.get_last_log(PROJECT)
            if last.get("status") in {"PASS", "FAIL", "TIMEOUT", "INTERRUPTED", "LAUNCH_FAILED"}:
                return last
            time.sleep(0.03)
        raise AssertionError(f"verification did not finish: {last}")

    def prepare_worker_starting(self) -> tuple[int, str, str, str]:
        store = self.store
        lock_fd = _try_acquire_lock(store)
        if lock_fd is None:
            raise AssertionError("fixture project lock unexpectedly busy")
        run_id = uuid.uuid4().hex
        started_at = _utc_now()
        head = self.head()
        starting = _state_template(
            project=PROJECT,
            run_id=run_id,
            status="STARTING",
            head=head,
            started_at=started_at,
            timeout_seconds=self.runtime._profile(PROJECT).timeout_seconds,
            launcher_pid=os.getpid(),
        )
        store.write_state(starting)
        store.prepare_log()
        starting["log_run_id"] = run_id
        store.write_state(starting)
        return lock_fd, run_id, head, started_at


class FinalHardeningTests(unittest.TestCase):
    def _minimal_runtime_for_state(self) -> tuple[AgentRuntime, StateStore, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state_dir = root / "state"
        checkout = root / "checkout"
        config = root / "runtime.toml"
        config.write_text(
            f'''version = 1\nstate_dir = {json.dumps(str(state_dir))}\n\n[projects.{PROJECT}]\nrepository = "owner/repo"\ncheckout = {json.dumps(str(checkout))}\nremote = "origin"\nexpected_remote_url = "unused"\nbranch = "main"\nverify_argv = ["./verify"]\ntimeout_seconds = 5\ndisposable = true\n''',
            encoding="utf-8",
        )
        runtime = AgentRuntime(config)
        return runtime, runtime._store(PROJECT), root

    def _valid_pass_state(self) -> dict:
        started = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 8, 24, 0, 0, 1, tzinfo=timezone.utc)
        return {
            "version": 1,
            "project": PROJECT,
            "run_id": PASS_RUN_ID,
            "status": "PASS",
            "head": "a" * 40,
            "launcher_pid": 101,
            "worker_pid": 102,
            "verify_pid": 103,
            "verify_pgid": 103,
            "timeout_seconds": 5,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": 1.0,
            "exit_code": 0,
            "timed_out": False,
            "verification_ok": True,
            "failure_kind": None,
            "working_tree_after": {
                "ok": True,
                "head": "a" * 40,
                "cached_remote_head": "a" * 40,
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
            "log_run_id": PASS_RUN_ID,
            "log_finalized": True,
        }

    def test_H1_public_get_last_log_rejects_semantically_false_passes(self) -> None:
        cases = {
            "nonzero_exit": lambda s: s.update(exit_code=7),
            "missing_postcondition": lambda s: s["postconditions"].pop("clean"),
            "false_postcondition": lambda s: s["postconditions"].update(clean=False),
            "log_run_mismatch": lambda s: s.update(log_run_id="f" * 32),
            "log_not_finalized": lambda s: s.update(log_finalized=False),
            "wrong_version": lambda s: s.update(version=2),
            "timed_out_pass": lambda s: s.update(timed_out=True),
            "failure_kind_pass": lambda s: s.update(failure_kind="postcondition_failed"),
            "verification_false": lambda s: s.update(verification_ok=False),
            "verification_null": lambda s: s.update(verification_ok=None),
            "working_tree_clean_contradiction": lambda s: s["working_tree_after"].update(clean=False),
            "working_tree_head_mismatch": lambda s: s["working_tree_after"].update(head="b" * 40),
            "cached_remote_head_mismatch": lambda s: s["working_tree_after"].update(cached_remote_head="b" * 40),
            "working_tree_branch_inconsistency": lambda s: s["working_tree_after"].update(current_branch="other"),
            "verify_pid_pgid_mismatch": lambda s: s.update(verify_pgid=s["verify_pid"] + 1),
            "invalid_launcher_pid": lambda s: s.update(launcher_pid=0),
            "missing_finalized_log": lambda s: None,
        }
        failures: list[tuple[str, dict]] = []
        for name, mutate in cases.items():
            runtime, store, _ = self._minimal_runtime_for_state()
            state = self._valid_pass_state()
            mutate(state)
            store.ensure_directory()
            if name != "missing_finalized_log":
                store.log_path.write_text("valid finalized diagnostics\n", encoding="utf-8")
            store.state_path.write_text(json.dumps(state), encoding="utf-8")
            result = runtime.get_last_log(PROJECT)
            if result.get("status") == "PASS" or result.get("verification_ok") is True:
                failures.append((name, result))
        print("H1_DIAGNOSTIC", [(name, r.get("status"), r.get("verification_ok")) for name, r in failures])
        self.assertEqual(failures, [])

    def test_H1_generated_valid_pass_remains_authoritative(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\nexit 0\n")
        launch = fx.runtime.run_verify(PROJECT)
        self.assertTrue(launch["accepted"])
        result = fx.wait_terminal()
        self.assertEqual(result["status"], "PASS")
        self.assertIs(result["verification_ok"], True)

    def test_H2_post_popen_running_state_failure_cleans_group_before_unlock(self) -> None:
        fx = RuntimeFixture(
            self,
            "#!/usr/bin/env bash\n"
            "echo $$ > leader.pid\n"
            "(sleep 60) &\n"
            "echo $! > child.pid\n"
            "sleep 60\n",
            timeout=20,
        )
        lock_fd, run_id, head, started_at = fx.prepare_worker_starting()
        original_write = StateStore.write_state
        observed_pgid: int | None = None

        def fail_post_popen(store: StateStore, state: dict) -> None:
            nonlocal observed_pgid
            if state.get("status") == "RUNNING" and state.get("verify_pid") is not None:
                observed_pgid = int(state["verify_pgid"])
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if (fx.checkout / "leader.pid").exists() and (fx.checkout / "child.pid").exists():
                        break
                    time.sleep(0.01)
                raise OSError("injected post-Popen RUNNING persistence failure")
            original_write(store, state)

        leader_pid = child_pid = None
        reacquired = None
        try:
            with mock.patch.object(StateStore, "write_state", new=fail_post_popen):
                rc = _run_verify_worker(
                    config_path=fx.config,
                    project=PROJECT,
                    lock_fd=lock_fd,
                    run_id=run_id,
                    expected_head=head,
                    started_at=started_at,
                )
            self.assertNotEqual(rc, 0)
            leader_pid = int((fx.checkout / "leader.pid").read_text().strip())
            child_pid = int((fx.checkout / "child.pid").read_text().strip())
            pgid = observed_pgid or leader_pid
            leader_gone = wait_pid_gone(leader_pid, 1.0)
            child_gone = wait_pid_gone(child_pid, 1.0)
            group_gone = not _process_group_exists(pgid)
            reacquired = _try_acquire_lock(fx.store)
            print(
                "H2_DIAGNOSTIC",
                {"rc": rc, "leader_gone": leader_gone, "child_gone": child_gone, "group_gone": group_gone, "lock_reacquired": reacquired is not None},
            )
            self.assertTrue(leader_gone)
            self.assertTrue(child_gone)
            self.assertTrue(group_gone)
            self.assertIsNotNone(reacquired)
            state, _ = fx.store.read_state()
            self.assertFalse(state is not None and state.get("status") == "PASS")
        finally:
            if reacquired is not None:
                _close_fd(reacquired)
            if observed_pgid is not None and _process_group_exists(observed_pgid):
                try:
                    os.killpg(observed_pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if leader_pid is not None:
                wait_pid_gone(leader_pid, 1.0)

    def test_H2_terminal_persistence_failure_releases_lock_without_orphan(self) -> None:
        fx = RuntimeFixture(self, "#!/usr/bin/env bash\necho $$ > leader.pid\nexit 0\n")
        lock_fd, run_id, head, started_at = fx.prepare_worker_starting()
        with mock.patch.object(StateStore, "commit_terminal_with_log", side_effect=OSError("injected terminal persistence failure")):
            rc = _run_verify_worker(
                config_path=fx.config,
                project=PROJECT,
                lock_fd=lock_fd,
                run_id=run_id,
                expected_head=head,
                started_at=started_at,
            )
        self.assertNotEqual(rc, 0)
        state, _ = fx.store.read_state()
        pgid = int(state["verify_pgid"])
        self.assertFalse(_process_group_exists(pgid))
        reacquired = None
        lock_deadline = time.monotonic() + 1.0
        while time.monotonic() < lock_deadline and reacquired is None:
            reacquired = _try_acquire_lock(fx.store)
            if reacquired is None:
                time.sleep(0.02)
        self.assertIsNotNone(reacquired)
        if reacquired is not None:
            _close_fd(reacquired)
        self.assertNotEqual(state.get("status"), "PASS")

    def test_H3_verifier_receives_only_runtime_path_environment(self) -> None:
        secret = "hardening-secret-19f7b2c5"
        control = "control-plane-sentinel-e301"
        fx = RuntimeFixture(
            self,
            "#!/usr/bin/env bash\n"
            "env | sort\n"
            "if [[ -v AGENT_RUNTIME_TEST_SECRET || -v AGENT_RUNTIME_CONTROL_SENTINEL ]]; then exit 42; fi\n"
            "command -v bash >/dev/null\n"
            "git --version >/dev/null\n"
            "exit 0\n",
        )
        with mock.patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_TEST_SECRET": secret,
                "AGENT_RUNTIME_CONTROL_SENTINEL": control,
            },
            clear=False,
        ):
            launch = fx.runtime.run_verify(PROJECT)
        self.assertTrue(launch["accepted"])
        result = fx.wait_terminal()
        log_text = fx.store.log_path.read_text(encoding="utf-8", errors="replace")
        print(
            "H3_DIAGNOSTIC",
            {"status": result.get("status"), "secret_name": "AGENT_RUNTIME_TEST_SECRET" in log_text, "control_name": "AGENT_RUNTIME_CONTROL_SENTINEL" in log_text, "secret_value": secret in log_text},
        )
        self.assertEqual(result["status"], "PASS")
        self.assertIs(result["verification_ok"], True)
        self.assertNotIn("AGENT_RUNTIME_TEST_SECRET", log_text)
        self.assertNotIn("AGENT_RUNTIME_CONTROL_SENTINEL", log_text)
        self.assertNotIn(secret, log_text)
        self.assertNotIn(secret, repr(result))
        self.assertIn("PATH=", log_text)

    def test_M1_persisted_verifier_output_is_hard_capped_and_overflow_fails(self) -> None:
        fx = RuntimeFixture(
            self,
            "#!/usr/bin/env bash\n"
            "echo $$ > leader.pid\n"
            "printf '%2097152s' ''\n"
            "sleep 30\n",
            timeout=4,
        )
        started = time.monotonic()
        launch = fx.runtime.run_verify(PROJECT)
        self.assertTrue(launch["accepted"])
        result = fx.wait_terminal(timeout=8)
        elapsed = time.monotonic() - started
        log_size = fx.store.log_path.stat().st_size
        leader_pid = int((fx.checkout / "leader.pid").read_text().strip())
        group_gone = not _process_group_exists(leader_pid)
        print(
            "M1_DIAGNOSTIC",
            {"elapsed": round(elapsed, 3), "log_size": log_size, "status": result.get("status"), "failure_kind": result.get("failure_kind"), "group_gone": group_gone, "public_bytes": result.get("log_bytes_returned")},
        )
        self.assertLess(elapsed, 3.0)
        self.assertLessEqual(log_size, LOG_LIMIT)
        self.assertNotEqual(result["status"], "PASS")
        self.assertIs(result["verification_ok"], False)
        self.assertEqual(result["failure_kind"], "verify_log_limit_exceeded")
        self.assertTrue(group_gone)
        self.assertIn("VERIFY LOG LIMIT EXCEEDED", result["log_tail"])
        self.assertLessEqual(result["log_bytes_returned"], MAX_LOG_TAIL_BYTES)
        reacquired = None
        lock_deadline = time.monotonic() + 1.0
        while time.monotonic() < lock_deadline and reacquired is None:
            reacquired = _try_acquire_lock(fx.store)
            if reacquired is None:
                time.sleep(0.02)
        self.assertIsNotNone(reacquired)
        if reacquired is not None:
            _close_fd(reacquired)


if __name__ == "__main__":
    unittest.main()
