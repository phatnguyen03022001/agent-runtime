from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import capacity, server
from agent_runtime.capacity import HeavyExecutionAdmission
from agent_runtime.durable_pipe import (
    DurableSnapshot,
    DurableStore,
    durable_job_id,
    verify_process_identity,
)
from agent_runtime.errors import RuntimeCapacityError, RuntimeStateError
from agent_runtime.session import TerminalSessionManager
from agent_runtime.tool_contract import EffectState, SafeNextAction


class DurablePipeRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-runtime-restart-")
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
        admission: HeavyExecutionAdmission | None = None,
        limit: int = 2,
        hard_wall: float = 5.0,
        retention: float = 3600.0,
    ) -> tuple[TerminalSessionManager, HeavyExecutionAdmission]:
        chosen = admission or HeavyExecutionAdmission(limit)
        manager = TerminalSessionManager(
            admission=chosen,
            max_active_sessions=limit,
            running_hard_wall_seconds=hard_wall,
            completed_retention_seconds=retention,
            durable_state_root=self.state_root,
            start_reaper=False,
        )
        self.managers.append(manager)
        return manager, chosen

    def remember_owners(self, start_identity: str) -> dict[str, object]:
        snapshot = DurableStore(self.state_root).read_for_identity(start_identity)
        runner = snapshot.state["runner_identity"]
        process = snapshot.state["process_identity"]
        if isinstance(runner, dict):
            self.runner_identities.append(dict(runner))
        if isinstance(process, dict):
            self.process_identities.append(dict(process))
        return snapshot.state

    def poll_until_text(
        self,
        manager: TerminalSessionManager,
        identity: str,
        text: str,
        *,
        cursor: int = 0,
        timeout: float = 4.0,
    ) -> tuple[dict[str, object], int, str]:
        observed = ""
        last: dict[str, object] | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            last = manager.poll(
                start_identity=identity,
                cursor=cursor,
                wait_ms=100,
                wait_for="output_or_state",
            )
            observed += "".join(
                chunk["text"] for chunk in (last.get("output_chunks") or [])
            )
            cursor = int(last["next_cursor"])
            if text in observed:
                return last, cursor, observed
        self.fail(f"output {text!r} not observed; last={last!r} observed={observed!r}")

    def poll_terminal(
        self,
        manager: TerminalSessionManager,
        identity: str,
        *,
        cursor: int = 0,
        timeout: float = 5.0,
    ) -> tuple[dict[str, object], int, str]:
        observed = ""
        last: dict[str, object] | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            last = manager.poll(
                start_identity=identity,
                cursor=cursor,
                wait_ms=100,
                wait_for="terminal_or_deadline",
            )
            observed += "".join(
                chunk["text"] for chunk in (last.get("output_chunks") or [])
            )
            cursor = int(last["next_cursor"])
            if last["status"] == "exited":
                return last, cursor, observed
        self.fail(f"durable session did not exit; last={last!r} observed={observed!r}")

    def test_clean_restart_preserves_cursor_output_capacity_and_completed_recovery(self) -> None:
        identity = "a" * 32
        argv = [
            sys.executable,
            "-u",
            "-c",
            "import time; print('before', flush=True); time.sleep(.45); print('after', flush=True)",
        ]
        manager_a, admission_a = self.manager(limit=2)
        started = manager_a.start(
            argv,
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        first, cursor, before = self.poll_until_text(manager_a, identity, "before\n")
        self.assertEqual(first["session_id"], started["session_id"])
        self.assertEqual(admission_a.active, 1)

        manager_a.shutdown()
        self.assertEqual(admission_a.active, 0)

        manager_b, admission_b = self.manager(limit=2)
        self.assertIsNone(manager_b.recovery_reason())
        self.assertEqual(manager_b.active_session_count(), 1)
        self.assertEqual(admission_b.active, 1)
        by_session = manager_b.poll(
            session_id=str(started["session_id"]),
            cursor=cursor,
            wait_ms=0,
            output="none",
        )
        self.assertEqual(by_session["session_id"], started["session_id"])
        self.assertEqual(by_session["next_cursor"], cursor)
        final, cursor, after = self.poll_terminal(manager_b, identity, cursor=cursor)
        self.assertEqual(final["session_id"], started["session_id"])
        self.assertNotIn("before\n", after)
        self.assertIn("after\n", after)
        self.assertIn("before\n", before)
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(final["termination_reason"], "natural_exit")
        self.assertEqual(final["durability"], "runtime_restart")
        self.assertEqual(admission_b.active, 0)

        manager_b.shutdown()
        manager_c, admission_c = self.manager(limit=2)
        recovered = manager_c.poll(start_identity=identity, cursor=cursor, wait_ms=0)
        self.assertEqual(recovered["session_id"], started["session_id"])
        self.assertEqual(recovered["status"], "exited")
        self.assertEqual(recovered["exit_code"], 0)
        self.assertEqual(admission_c.active, 0)

    def test_abrupt_runtime_kill_lost_ack_repeat_joins_one_effect(self) -> None:
        identity = "b" * 32
        marker = self.root / "effects.txt"
        target_code = (
            "import pathlib,sys,time; "
            "p=pathlib.Path(sys.argv[1]); "
            "f=p.open('a'); f.write('effect\\n'); f.close(); "
            "print('started', flush=True); time.sleep(.8); print('done', flush=True)"
        )
        argv = [sys.executable, "-u", "-c", target_code, str(marker)]
        runtime_code = (
            "import os,signal,sys; "
            "from agent_runtime.capacity import HeavyExecutionAdmission; "
            "from agent_runtime.session import TerminalSessionManager; "
            "state,cwd,identity,marker,target=sys.argv[1:]; "
            "m=TerminalSessionManager(admission=HeavyExecutionAdmission(2), "
            "max_active_sessions=2, running_hard_wall_seconds=5.0, "
            "durable_state_root=state, start_reaper=False); "
            "m.start([sys.executable,'-u','-c',target,marker],cwd,identity,'pipe','runtime_restart'); "
            "os.kill(os.getpid(), signal.SIGKILL)"
        )
        env = dict(os.environ)
        env["AGENT_RUNTIME_WORKSPACE_ROOT"] = str(self.cwd)
        runtime = subprocess.Popen(
            [
                sys.executable,
                "-c",
                runtime_code,
                str(self.state_root),
                str(self.cwd),
                identity,
                str(marker),
                target_code,
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
        stdout, stderr = runtime.communicate(timeout=10)
        self.assertEqual(runtime.returncode, -signal.SIGKILL, (stdout, stderr))

        state = self.remember_owners(identity)
        manager_b, admission_b = self.manager(limit=2)
        joined = manager_b.start(
            argv,
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.assertEqual(joined["session_id"], state["session_id"])
        self.assertEqual(admission_b.active, 1 if joined["status"] != "exited" else 0)
        final, _cursor, output = self.poll_terminal(manager_b, identity)
        self.assertEqual(final["session_id"], state["session_id"])
        self.assertEqual(final["exit_code"], 0)
        self.assertIn("done\n", output)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["effect"])

    def test_different_spec_identity_conflict_does_not_launch_second_effect(self) -> None:
        identity = "c" * 32
        marker = self.root / "effects.txt"
        first_code = (
            "import pathlib,sys,time; "
            "p=pathlib.Path(sys.argv[1]); p.write_text('one\\n'); "
            "print('ready', flush=True); time.sleep(3)"
        )
        manager, _admission = self.manager()
        manager.start(
            [sys.executable, "-u", "-c", first_code, str(marker)],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        self.poll_until_text(manager, identity, "ready\n")

        with self.assertRaises(RuntimeStateError) as raised:
            manager.start(
                [sys.executable, "-u", "-c", "print('different')"],
                str(self.cwd),
                identity,
                "pipe",
                "runtime_restart",
            )
        self.assertEqual(raised.exception.reason_code, "START_IDENTITY_CONFLICT")
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["one"])

    def test_hard_wall_deadline_survives_restart(self) -> None:
        identity = "d" * 32
        manager_a, _admission_a = self.manager(limit=2, hard_wall=0.7)
        started_at = time.monotonic()
        manager_a.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready', flush=True); time.sleep(10)",
            ],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        _ready, cursor, _ = self.poll_until_text(manager_a, identity, "ready\n")
        time.sleep(0.25)
        manager_a.shutdown()

        manager_b, _admission_b = self.manager(limit=2, hard_wall=0.7)
        final, _cursor, _output = self.poll_terminal(
            manager_b,
            identity,
            cursor=cursor,
            timeout=3.0,
        )
        elapsed = time.monotonic() - started_at
        self.assertEqual(final["termination_reason"], "hard_wall_timeout")
        self.assertLess(elapsed, 1.8)

    def test_leader_exit_with_pipe_holding_descendant_finalizes_without_false_owner_loss(self) -> None:
        identity = "5" * 32
        descendant_code = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(5)"
        )
        code = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{descendant_code!r}]); "
            "print('leader-exit', flush=True)"
        )
        manager, admission = self.manager()
        started = time.monotonic()
        result = manager.start(
            [sys.executable, "-u", "-c", code],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        final, _cursor, output = self.poll_terminal(manager, identity, timeout=4.0)
        self.assertEqual(final["session_id"], result["session_id"])
        self.assertEqual(final["termination_reason"], "natural_exit")
        self.assertIn("leader-exit\n", output)
        self.assertEqual(admission.active, 0)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_terminate_after_restart_revalidates_and_finalizes(self) -> None:
        identity = "e" * 32
        manager_a, admission_a = self.manager(limit=2)
        started = manager_a.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready', flush=True); time.sleep(10)",
            ],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        self.poll_until_text(manager_a, identity, "ready\n")
        manager_a.shutdown()
        self.assertEqual(admission_a.active, 0)

        manager_b, admission_b = self.manager(limit=2)
        self.assertEqual(admission_b.active, 1)
        control = manager_b.control(str(started["session_id"]), "terminate")
        self.assertIn(control["status"], {"running", "exited"})
        final, _cursor, _output = self.poll_terminal(manager_b, identity)
        self.assertEqual(final["termination_reason"], "explicit_terminate")
        self.assertEqual(admission_b.active, 0)

    def test_recovered_job_counts_in_capacity_observer_and_releases_once(self) -> None:
        identity = "f" * 32
        manager_a, _admission_a = self.manager(limit=1)
        started = manager_a.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready', flush=True); time.sleep(10)",
            ],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        self.remember_owners(identity)
        self.poll_until_text(manager_a, identity, "ready\n")
        manager_a.shutdown()

        admission_b = HeavyExecutionAdmission(1)
        manager_b, _ = self.manager(admission=admission_b, limit=1)
        self.assertEqual(admission_b.active, 1)
        self.assertEqual(manager_b.active_session_count(), 1)

        with (
            patch.object(server, "heavy_execution_admission", return_value=admission_b),
            patch.object(
                server,
                "active_terminal_session_count",
                side_effect=manager_b.active_session_count,
            ),
            patch.object(capacity, "_collect_signals", side_effect=OSError("unavailable")),
        ):
            snapshot = server._operational_capacity_snapshot()
            observed = capacity.observe_capacity(server._operational_capacity_snapshot)
        self.assertEqual(snapshot, (1, 1, 1))
        self.assertEqual(observed["active_heavy"], 1)
        self.assertEqual(observed["available_heavy"], 0)
        self.assertEqual(observed["active_sessions"], 1)

        with self.assertRaises(RuntimeCapacityError):
            manager_b.start(
                [sys.executable, "-c", "pass"],
                str(self.cwd),
                "0" * 32,
                "pipe",
            )

        manager_b.control(str(started["session_id"]), "terminate")
        final, _cursor, _output = self.poll_terminal(manager_b, identity)
        self.assertEqual(final["termination_reason"], "explicit_terminate")
        self.assertEqual(admission_b.active, 0)
        manager_b.reap_once()
        self.assertEqual(admission_b.active, 0)

    def test_recovery_over_capacity_fails_closed_without_killing_jobs(self) -> None:
        identities = ("1" * 32, "2" * 32)
        manager_a, _admission_a = self.manager(limit=2)
        for identity in identities:
            manager_a.start(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(10)",
                ],
                str(self.cwd),
                identity,
                "pipe",
                "runtime_restart",
            )
            self.remember_owners(identity)
            self.poll_until_text(manager_a, identity, "ready\n")
        manager_a.shutdown()

        admission_b = HeavyExecutionAdmission(1)
        manager_b, _ = self.manager(admission=admission_b, limit=2)
        self.assertEqual(manager_b.recovery_reason(), "DURABLE_RECOVERY_OVER_CAPACITY")
        self.assertEqual(admission_b.active, 1)
        self.assertEqual(manager_b.active_session_count(), 2)
        self.assertTrue(all(verify_process_identity(i) for i in self.process_identities[-2:]))

        with self.assertRaises(RuntimeStateError) as raised:
            manager_b.start(
                [sys.executable, "-c", "pass"],
                str(self.cwd),
                "3" * 32,
                "pipe",
            )
        error = raised.exception
        self.assertEqual(error.reason_code, "DURABLE_RECOVERY_OVER_CAPACITY")
        self.assertTrue(error.reconciliation_required)
        self.assertEqual(error.safe_next_action, SafeNextAction.RECONCILE)
        self.assertTrue(all(verify_process_identity(i) for i in self.process_identities[-2:]))

    def test_finalization_race_reconciles_terminal_state_without_respawn(self) -> None:
        identity = "6" * 32
        manager, _admission = self.manager()
        started = manager.start(
            [sys.executable, "-u", "-c", "print('done', flush=True)"],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )

        store = DurableStore(self.state_root)
        job_id = durable_job_id(identity)
        deadline = time.monotonic() + 3.0
        terminal = None
        while time.monotonic() < deadline:
            snapshot = store.read_snapshot(str(job_id))
            if snapshot.state["status"] == "exited":
                terminal = snapshot
                break
            time.sleep(0.01)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.state["lifecycle"], "COMPLETED")
        self.assertEqual(terminal.state["exit_code"], 0)

        owner_deadline = time.monotonic() + 1.0
        while time.monotonic() < owner_deadline and (
            verify_process_identity(terminal.state["runner_identity"])
            or verify_process_identity(terminal.state["process_identity"])
        ):
            time.sleep(0.01)
        self.assertFalse(verify_process_identity(terminal.state["runner_identity"]))
        self.assertFalse(verify_process_identity(terminal.state["process_identity"]))

        stale_state = dict(terminal.state)
        stale_state.update(
            status="running",
            lifecycle="RUNNING",
            exit_code=None,
            termination_reason=None,
            completed_at_epoch=None,
        )
        stale = DurableSnapshot(state=stale_state, chunks=terminal.chunks)

        with (
            patch.object(
                manager._durable_store,
                "read_snapshot",
                side_effect=[stale, terminal, terminal],
            ) as read_snapshot,
            patch("agent_runtime.session.subprocess.Popen") as popen,
        ):
            by_identity = manager.poll(start_identity=identity, cursor=0, wait_ms=0)
            by_session = manager.poll(
                session_id=str(started["session_id"]),
                cursor=0,
                wait_ms=0,
            )

        self.assertEqual(by_identity["session_id"], started["session_id"])
        self.assertEqual(by_session["session_id"], started["session_id"])
        self.assertEqual(by_identity["status"], "exited")
        self.assertEqual(by_session["status"], "exited")
        self.assertEqual(by_identity["exit_code"], 0)
        self.assertEqual(by_session["exit_code"], 0)
        self.assertEqual(read_snapshot.call_count, 3)
        popen.assert_not_called()

    def test_runner_disappearance_is_owner_lost_without_respawn_or_signal(self) -> None:
        identity = "4" * 32
        manager_a, _admission_a = self.manager()
        started = manager_a.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready', flush=True); time.sleep(10)",
            ],
            str(self.cwd),
            identity,
            "pipe",
            "runtime_restart",
        )
        state = self.remember_owners(identity)
        self.poll_until_text(manager_a, identity, "ready\n")
        runner = dict(state["runner_identity"])
        process = dict(state["process_identity"])
        self.assertTrue(verify_process_identity(runner))
        self.assertTrue(verify_process_identity(process))
        os.kill(runner["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and verify_process_identity(runner):
            time.sleep(0.01)
        self.assertFalse(verify_process_identity(runner))
        self.assertTrue(verify_process_identity(process))
        manager_a.shutdown()

        manager_b, _admission_b = self.manager()
        with self.assertRaises(RuntimeStateError) as raised:
            manager_b.poll(start_identity=identity, cursor=0, wait_ms=0)
        error = raised.exception
        self.assertEqual(error.reason_code, "DURABLE_OWNER_LOST")
        self.assertEqual(error.effect_state, EffectState.UNKNOWN)
        self.assertTrue(error.reconciliation_required)
        self.assertEqual(error.safe_next_action, SafeNextAction.RECONCILE)
        self.assertTrue(verify_process_identity(process))

        with self.assertRaises(RuntimeStateError) as control_error:
            manager_b.control(str(started["session_id"]), "terminate")
        self.assertEqual(control_error.exception.reason_code, "DURABLE_OWNER_LOST")
        self.assertTrue(verify_process_identity(process))


if __name__ == "__main__":
    unittest.main()
