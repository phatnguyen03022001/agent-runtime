from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.capacity import HeavyExecutionAdmission
from agent_runtime.durable_pipe import (
    DURABLE_SCAN_LIMIT,
    MAX_RETAINED_OUTPUT_BYTES,
    DurableStateCorrupt,
    DurableStore,
    durable_job_id,
    observe_process_identity,
    verify_process_identity,
)
from agent_runtime.errors import RuntimeStateError, RuntimeValidationError
from agent_runtime.session import TerminalSessionManager
from agent_runtime.tool_contract import EffectState, SafeNextAction


class DurablePipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-runtime-durable-")
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.state_root = self.root / "durable-jobs"
        self.env_patch = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.cwd)},
            clear=False,
        )
        self.env_patch.start()
        self.managers: list[TerminalSessionManager] = []
        self.runner_identities: list[dict[str, int]] = []
        self.process_identities: list[dict[str, int]] = []

    def tearDown(self) -> None:
        for manager in reversed(self.managers):
            try:
                manager.shutdown()
            except Exception:
                pass
        for identity in reversed(self.process_identities):
            if verify_process_identity(identity):
                try:
                    os.killpg(identity["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for identity in reversed(self.runner_identities):
            if verify_process_identity(identity):
                try:
                    os.kill(identity["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(
            verify_process_identity(identity)
            for identity in self.process_identities + self.runner_identities
        ):
            time.sleep(0.01)
        self.env_patch.stop()
        self.temp.cleanup()

    def manager(
        self,
        *,
        limit: int = 2,
        hard_wall: float = 5.0,
        retention: float = 3600.0,
    ) -> TerminalSessionManager:
        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(limit),
            max_active_sessions=limit,
            running_hard_wall_seconds=hard_wall,
            completed_retention_seconds=retention,
            durable_state_root=self.state_root,
            start_reaper=False,
        )
        self.managers.append(manager)
        return manager

    def remember_owners(self, start_identity: str) -> None:
        snapshot = DurableStore(self.state_root).read_for_identity(start_identity)
        runner = snapshot.state["runner_identity"]
        process = snapshot.state["process_identity"]
        if isinstance(runner, dict):
            self.runner_identities.append(dict(runner))
        if isinstance(process, dict):
            self.process_identities.append(dict(process))

    def wait_terminal(
        self,
        manager: TerminalSessionManager,
        start_identity: str,
        *,
        timeout: float = 4.0,
        cursor: int = 0,
    ) -> tuple[dict[str, object], list[dict[str, str]], int]:
        chunks: list[dict[str, str]] = []
        deadline = time.monotonic() + timeout
        last: dict[str, object] | None = None
        while time.monotonic() < deadline:
            last = manager.poll(
                start_identity=start_identity,
                cursor=cursor,
                wait_ms=100,
                wait_for="output_or_state",
            )
            chunks.extend(last.get("output_chunks") or [])
            cursor = int(last["next_cursor"])
            if last["status"] == "exited":
                return last, chunks, cursor
        self.fail(f"durable session did not exit; last={last!r}")

    def test_native_process_identity_rejects_instance_mismatch(self) -> None:
        identity = observe_process_identity(os.getpid())
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertTrue(verify_process_identity(identity))

        mismatch = dict(identity)
        mismatch["start_usec"] += 1
        self.assertFalse(verify_process_identity(mismatch))

    def test_runner_argv_is_opaque_and_durable_files_are_private(self) -> None:
        manager = self.manager()
        identity = "1" * 32
        secret = "task0153-user-content-must-not-reach-runner-argv"
        argv = [
            sys.executable,
            "-u",
            "-c",
            f"import time; print({secret!r}, flush=True); time.sleep(2)",
        ]
        real_popen = subprocess.Popen
        observed: list[list[str]] = []

        def capture_popen(args, *pargs, **kwargs):
            observed.append(list(args))
            return real_popen(args, *pargs, **kwargs)

        with patch("agent_runtime.session.subprocess.Popen", new=capture_popen):
            result = manager.start(
                argv,
                str(self.cwd),
                identity,
                "pipe",
                "runtime_restart",
            )

        self.assertEqual(result["durability"], "runtime_restart")
        self.assertEqual(len(observed), 1)
        runner_argv = observed[0]
        self.assertEqual(runner_argv[-1], durable_job_id(identity))
        self.assertNotIn(secret, " ".join(runner_argv))
        self.assertNotIn(str(self.cwd), " ".join(runner_argv))

        self.remember_owners(identity)
        job_dir = self.state_root / durable_job_id(identity)
        self.assertEqual(stat.S_IMODE(os.lstat(self.state_root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(job_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(job_dir / "state.json").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(job_dir / "journal.json").st_mode), 0o600)
        self.assertFalse((job_dir / "spec.json").exists())

        with self.assertRaises(RuntimeValidationError) as write_error:
            manager.control(str(result["session_id"]), "write", data="x")
        self.assertEqual(write_error.exception.reason_code, "PIPE_WRITE_UNSUPPORTED")
        with self.assertRaises(RuntimeValidationError) as resize_error:
            manager.control(
                str(result["session_id"]),
                "resize",
                rows=24,
                cols=80,
            )
        self.assertEqual(resize_error.exception.reason_code, "PTY_REQUIRED")

        manager.control(str(result["session_id"]), "terminate")
        final, _chunks, _cursor = self.wait_terminal(manager, identity)
        self.assertEqual(final["termination_reason"], "explicit_terminate")

    def test_corrupt_partial_state_projects_unknown_reconciliation(self) -> None:
        identity = "2" * 32
        job_id = durable_job_id(identity)
        self.state_root.mkdir(mode=0o700)
        job_dir = self.state_root / job_id
        job_dir.mkdir(mode=0o700)
        state_path = job_dir / "state.json"
        state_path.write_text("{}", encoding="utf-8")
        os.chmod(state_path, 0o600)

        manager = self.manager()
        with self.assertRaises(RuntimeStateError) as raised:
            manager.poll(session_id="missing-session")
        error = raised.exception
        self.assertEqual(error.reason_code, "DURABLE_STATE_CORRUPT")
        self.assertEqual(error.effect_state, EffectState.UNKNOWN)
        self.assertTrue(error.reconciliation_required)
        self.assertEqual(error.safe_next_action, SafeNextAction.RECONCILE)
        self.assertTrue(job_dir.exists())

    def test_symlink_and_permission_attacks_fail_closed(self) -> None:
        identity = "3" * 32
        job_id = durable_job_id(identity)
        self.state_root.mkdir(mode=0o700)
        job_dir = self.state_root / job_id
        job_dir.mkdir(mode=0o700)
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        os.chmod(outside, 0o600)
        state_path = job_dir / "state.json"
        state_path.symlink_to(outside)

        store = DurableStore(self.state_root)
        with self.assertRaises(DurableStateCorrupt):
            store.read_snapshot(job_id)

        state_path.unlink()
        state_path.write_text("{}", encoding="utf-8")
        os.chmod(state_path, 0o644)
        with self.assertRaises(DurableStateCorrupt):
            store.read_snapshot(job_id)

        os.chmod(self.state_root, 0o755)
        with self.assertRaises(DurableStateCorrupt):
            store.scan_job_ids()

    def test_snapshot_retry_accepts_valid_atomic_state_replacement(self) -> None:
        manager = self.manager()
        identity = "9" * 32
        manager.start(
            [sys.executable, "-u", "-c", "print('stable', flush=True)"],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        self.wait_terminal(manager, identity)

        store = DurableStore(self.state_root)
        job_id = durable_job_id(identity)
        job_dir = self.state_root / job_id
        state_path = job_dir / "state.json"
        baseline = store.read_snapshot(job_id)
        replacement_path = job_dir / ".state.json.regression-replacement"
        replacement_path.write_bytes(state_path.read_bytes())
        os.chmod(replacement_path, 0o600)

        real_open = os.open
        state_open_count = 0

        def replace_between_lstat_and_open(path, flags, *args, **kwargs):
            nonlocal state_open_count
            if Path(path) == state_path:
                state_open_count += 1
                if state_open_count == 1:
                    os.replace(replacement_path, state_path)
            return real_open(path, flags, *args, **kwargs)

        with patch("agent_runtime.durable_pipe.os.open", new=replace_between_lstat_and_open):
            snapshot = store.read_snapshot(job_id)

        self.assertEqual(snapshot.state["start_identity"], identity)
        self.assertEqual(
            snapshot.state["journal_generation"],
            baseline.state["journal_generation"],
        )
        self.assertEqual(snapshot.chunks, baseline.chunks)
        self.assertEqual(state_open_count, 3)

        replacement_after_open = job_dir / ".state.json.regression-after-open"
        replacement_after_open.write_bytes(state_path.read_bytes())
        os.chmod(replacement_after_open, 0o600)
        after_open_count = 0

        def replace_after_open_before_fstat(path, flags, *args, **kwargs):
            nonlocal after_open_count
            fd = real_open(path, flags, *args, **kwargs)
            if Path(path) == state_path:
                after_open_count += 1
                if after_open_count == 1:
                    os.replace(replacement_after_open, state_path)
            return fd

        with patch("agent_runtime.durable_pipe.os.open", new=replace_after_open_before_fstat):
            after_open_snapshot = store.read_snapshot(job_id)

        self.assertEqual(
            after_open_snapshot.state["journal_generation"],
            baseline.state["journal_generation"],
        )
        self.assertEqual(after_open_snapshot.chunks, baseline.chunks)
        self.assertEqual(after_open_count, 3)

        replacement_during_lstat = job_dir / ".state.json.regression-during-lstat"
        replacement_during_lstat.write_bytes(state_path.read_bytes())
        os.chmod(replacement_during_lstat, 0o600)
        real_lstat = os.lstat
        lstat_race_count = 0

        def replace_while_lstat_holds_old_inode(path, *args, **kwargs):
            nonlocal lstat_race_count
            if Path(path) == state_path:
                lstat_race_count += 1
                if lstat_race_count == 1:
                    fd = real_open(path, os.O_RDONLY)
                    try:
                        os.replace(replacement_during_lstat, state_path)
                        return os.fstat(fd)
                    finally:
                        os.close(fd)
            return real_lstat(path, *args, **kwargs)

        with patch("agent_runtime.durable_pipe.os.lstat", new=replace_while_lstat_holds_old_inode):
            during_lstat_snapshot = store.read_snapshot(job_id)

        self.assertEqual(
            during_lstat_snapshot.state["journal_generation"],
            baseline.state["journal_generation"],
        )
        self.assertEqual(during_lstat_snapshot.chunks, baseline.chunks)
        self.assertGreaterEqual(lstat_race_count, 2)

    def test_snapshot_consistency_still_rejects_mixed_generation_and_integrity_corruption(self) -> None:
        manager = self.manager()
        identity = "8" * 32
        manager.start(
            [sys.executable, "-u", "-c", "print('stable', flush=True)"],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        self.wait_terminal(manager, identity)

        store = DurableStore(self.state_root)
        job_id = durable_job_id(identity)
        job_dir = self.state_root / job_id
        snapshot = store.read_snapshot(job_id)

        journal_path = job_dir / "journal.json"
        journal_raw = journal_path.read_bytes()
        generation = int(snapshot.state["journal_generation"])
        mixed_journal = journal_raw.replace(
            f'"generation":{generation}'.encode(),
            f'"generation":{generation + 1}'.encode(),
            1,
        )
        self.assertNotEqual(mixed_journal, journal_raw)
        journal_path.write_bytes(mixed_journal)
        with self.assertRaisesRegex(DurableStateCorrupt, "changed inconsistently"):
            store.read_snapshot(job_id)
        journal_path.write_bytes(journal_raw)

        state_path = job_dir / "state.json"
        state_raw = state_path.read_bytes()
        tampered_state = state_raw.replace(identity.encode(), ("7" * 32).encode(), 1)
        self.assertNotEqual(tampered_state, state_raw)
        state_path.write_bytes(tampered_state)
        with self.assertRaisesRegex(DurableStateCorrupt, "integrity"):
            store.read_snapshot(job_id)

    def test_output_is_bounded_cursor_expiry_is_truthful_and_retention_cleans(self) -> None:
        manager = self.manager(retention=0.0)
        identity = "4" * 32
        payload_size = MAX_RETAINED_OUTPUT_BYTES + 8192
        code = (
            "import os; "
            f"os.write(1, b'x'*{payload_size}); "
            "os.write(2, b'ERR\\n'); "
            "os.write(1, b'END\\n')"
        )
        result = manager.start(
            [sys.executable, "-u", "-c", code],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)

        deadline = time.monotonic() + 4.0
        final: dict[str, object] | None = None
        while time.monotonic() < deadline:
            current = manager.poll(
                start_identity=identity,
                cursor=0,
                wait_ms=100,
                wait_for="terminal_or_deadline",
                output="none",
            )
            if current["status"] == "exited":
                final = current
                break
        self.assertIsNotNone(final)

        cursor = 0
        retained: list[dict[str, str]] = []
        first = manager.poll(start_identity=identity, cursor=cursor, wait_ms=0)
        self.assertTrue(first["cursor_expired"])
        self.assertGreater(first["dropped_output_bytes"], 0)
        while True:
            retained.extend(first["output_chunks"])
            next_cursor = int(first["next_cursor"])
            if next_cursor == cursor:
                break
            cursor = next_cursor
            first = manager.poll(start_identity=identity, cursor=cursor, wait_ms=0)
        text = "".join(chunk["text"] for chunk in retained)
        self.assertIn("END\n", text)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_RETAINED_OUTPUT_BYTES)

        store = DurableStore(self.state_root)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and store.job_exists_for_identity(identity):
            manager.reap_once()
            time.sleep(0.02)
        self.assertFalse(store.job_exists_for_identity(identity))
        self.assertFalse(manager.has_session(str(result["session_id"])))

    def test_recovery_scan_is_bounded_and_blocks_new_execution(self) -> None:
        self.state_root.mkdir(mode=0o700)
        for index in range(DURABLE_SCAN_LIMIT + 1):
            (self.state_root / f"{index:064x}").mkdir(mode=0o700)

        manager = self.manager()
        self.assertEqual(
            manager.recovery_reason(),
            "DURABLE_RECOVERY_OVER_CAPACITY",
        )
        with self.assertRaises(RuntimeStateError) as raised:
            manager.start(
                [sys.executable, "-c", "pass"],
                str(self.cwd),
                "7" * 32,
                "pipe",
            )
        self.assertEqual(
            raised.exception.reason_code,
            "DURABLE_RECOVERY_OVER_CAPACITY",
        )

    def test_process_default_and_pipe_only_durability_contract(self) -> None:
        manager = self.manager()
        process_identity = "5" * 32
        result = manager.start(
            [sys.executable, "-c", "pass"],
            str(self.cwd),
            process_identity,
            "pipe",
        )
        self.assertEqual(result["durability"], "process")
        final, _chunks, _cursor = self.wait_terminal(manager, process_identity)
        self.assertEqual(final["durability"], "process")

        with self.assertRaises(RuntimeValidationError) as raised:
            manager.start(
                [sys.executable, "-c", "pass"],
                str(self.cwd),
                "6" * 32,
                "pty",
                "runtime_restart",
            )
        self.assertEqual(raised.exception.reason_code, "DURABLE_PTY_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
