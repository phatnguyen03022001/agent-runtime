from __future__ import annotations

import json
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_runtime.session import TerminalSessionManager, control_terminal, poll_terminal, start_terminal
from agent_runtime.timing import bind_call_context, reset_call_context


class TerminalSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.env_patch = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
            clear=False,
        )
        self.env_patch.start()
        self.session_ids: list[str] = []

    def tearDown(self) -> None:
        for session_id in self.session_ids:
            try:
                control_terminal(session_id, "terminate")
            except (ValueError, ProcessLookupError, OSError):
                pass
        self.env_patch.stop()
        self.temp.cleanup()

    def start(self, argv: list[str]) -> dict[str, object]:
        result = start_terminal(argv, str(self.cwd))
        self.session_ids.append(str(result["session_id"]))
        return result

    def poll_until(self, session_id: str, predicate, timeout: float = 3.0):
        cursor = 0
        output = ""
        last = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            last = poll_terminal(session_id, cursor=cursor, wait_ms=100)
            cursor = last["next_cursor"]
            output += last["output"]
            if predicate(last, output):
                return last, output
        self.fail(f"session condition not reached; last={last!r} output={output!r}")

    def test_start_returns_before_long_running_child_exits_and_can_be_polled(self) -> None:
        started = time.monotonic()
        result = self.start(
            [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(5)"]
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["status"], "running")
        session_id = str(result["session_id"])
        _, output = self.poll_until(session_id, lambda _r, out: "ready" in out)
        self.assertIn("ready", output)

    def test_write_round_trips_utf8_input_over_the_pty(self) -> None:
        result = self.start(
            [sys.executable, "-u", "-c", "line=input(); print('got:'+line)"]
        )
        session_id = str(result["session_id"])
        control_terminal(session_id, "write", data="héllo\n")
        _, output = self.poll_until(session_id, lambda _r, out: "got:héllo" in out)
        self.assertIn("got:héllo", output)

    def test_interrupt_targets_the_process_group(self) -> None:
        code = (
            "import signal,sys,time; "
            "signal.signal(signal.SIGINT, lambda *_: sys.exit(23)); "
            "print('ready', flush=True); time.sleep(10)"
        )
        result = self.start([sys.executable, "-u", "-c", code])
        session_id = str(result["session_id"])
        self.poll_until(session_id, lambda _r, out: "ready" in out)
        control_terminal(session_id, "interrupt")
        final, _ = self.poll_until(session_id, lambda r, _out: r["status"] != "running")
        self.assertEqual(final["exit_code"], 23)

    def test_resize_changes_the_child_terminal_size(self) -> None:
        code = (
            "import os; print('ready', flush=True); input(); "
            "s=os.get_terminal_size(0); print(f'{s.lines}x{s.columns}', flush=True)"
        )
        result = self.start([sys.executable, "-u", "-c", code])
        session_id = str(result["session_id"])
        self.poll_until(session_id, lambda _r, out: "ready" in out)
        control_terminal(session_id, "resize", rows=40, cols=120)
        control_terminal(session_id, "write", data="\n")
        _, output = self.poll_until(session_id, lambda _r, out: "40x120" in out)
        self.assertIn("40x120", output)

    def test_control_rejects_invalid_actions_and_action_specific_arguments(self) -> None:
        result = self.start([sys.executable, "-u", "-c", "import time; time.sleep(5)"])
        session_id = str(result["session_id"])
        invalid = [
            ("unknown", {}),
            ("write", {}),
            ("write", {"data": 7}),
            ("write", {"data": "x", "rows": 1}),
            ("interrupt", {"data": "x"}),
            ("terminate", {"cols": 80}),
            ("resize", {}),
            ("resize", {"rows": 0, "cols": 80}),
            ("resize", {"rows": 24, "cols": -1}),
            ("resize", {"rows": 24, "cols": 80, "data": "x"}),
        ]
        for action, kwargs in invalid:
            with self.subTest(action=action, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    control_terminal(session_id, action, **kwargs)

    def test_persistent_child_uses_literal_argv_workspace_guard_and_minimal_environment(self) -> None:
        marker = self.cwd / "should-not-exist"
        literal = f"hello; touch {marker}"
        secret_names = {
            "CONTROL_PLANE_API_KEY": "secret-a",
            "AGENT_RUNTIME_INTERNAL": "secret-b",
            "OPENAI_API_KEY": "secret-c",
            "MY_TOKEN": "secret-d",
        }
        preserved = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "USER": "tester",
            "TMPDIR": str(self.root),
            "LANG": "C.UTF-8",
            "LC_TEST": "C",
        }
        env = {**secret_names, **preserved, "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)}
        names = sorted(set(secret_names) | set(preserved))
        code = (
            "import json,os,sys; "
            "print(sys.argv[1]); "
            f"print(json.dumps({{n: os.environ.get(n) for n in {names!r}}}))"
        )
        with patch.dict(os.environ, env, clear=True):
            result = start_terminal([sys.executable, "-u", "-c", code, literal], str(self.cwd))
            self.session_ids.append(str(result["session_id"]))
            final, output = self.poll_until(
                str(result["session_id"]), lambda r, _out: r["status"] != "running"
            )
        lines = [line.rstrip("\r") for line in output.splitlines() if line.strip()]
        self.assertEqual(lines[0], literal)
        observed = json.loads(lines[1])
        for name in secret_names:
            self.assertIsNone(observed[name], name)
        for name, value in preserved.items():
            self.assertEqual(observed[name], value, name)
        self.assertFalse(marker.exists())
        self.assertEqual(final["exit_code"], 0)

        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil_rmtree, outside)
        with self.assertRaisesRegex(ValueError, "outside"):
            start_terminal([sys.executable, "-c", "pass"], str(outside))

    def test_poll_cursor_is_incremental_and_wait_is_bounded(self) -> None:
        result = self.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('one'); time.sleep(.15); print('two'); time.sleep(.15)",
            ]
        )
        session_id = str(result["session_id"])
        first, first_output = self.poll_until(session_id, lambda _r, out: "one" in out)
        second, second_output = self.poll_until_from(
            session_id,
            first["next_cursor"],
            lambda _r, out: "two" in out,
        )
        self.assertNotIn("one", second_output)
        self.assertIn("two", second_output)
        empty = poll_terminal(session_id, cursor=second["next_cursor"], wait_ms=0)
        self.assertEqual(empty["output"], "")

        for invalid in (-1, 1001, True, 1.5):
            with self.subTest(wait_ms=invalid):
                with self.assertRaises(ValueError):
                    poll_terminal(session_id, cursor=0, wait_ms=invalid)  # type: ignore[arg-type]

    def poll_until_from(self, session_id: str, cursor: int, predicate, timeout: float = 3.0):
        output = ""
        last = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            last = poll_terminal(session_id, cursor=cursor, wait_ms=100)
            cursor = last["next_cursor"]
            output += last["output"]
            if predicate(last, output):
                return last, output
        self.fail(f"session condition not reached; last={last!r} output={output!r}")

    def test_poll_retention_and_response_are_bounded_and_cursor_expiry_is_truthful(self) -> None:
        from agent_runtime.session import MAX_POLL_OUTPUT_BYTES, MAX_RETAINED_OUTPUT_BYTES

        size = MAX_RETAINED_OUTPUT_BYTES + MAX_POLL_OUTPUT_BYTES + 8192
        result = self.start(
            [sys.executable, "-u", "-c", f"import sys,time; sys.stdout.write('x'*{size}); sys.stdout.flush(); time.sleep(.5)"]
        )
        session_id = str(result["session_id"])
        deadline = time.monotonic() + 3.0
        while True:
            polled = poll_terminal(session_id, cursor=0, wait_ms=100)
            if polled["cursor_expired"]:
                break
            if time.monotonic() >= deadline:
                self.fail("retention overflow condition was not observed before deadline")
        self.assertTrue(polled["cursor_expired"])
        self.assertGreater(polled["dropped_output_bytes"], 0)
        self.assertLessEqual(len(polled["output"].encode()), MAX_POLL_OUTPUT_BYTES)
        self.assertLess(polled["next_cursor"], size)

    def test_natural_exit_reports_exit_code_and_closes_pty(self) -> None:
        result = self.start([sys.executable, "-u", "-c", "print('done')"])
        session_id = str(result["session_id"])
        final, output = self.poll_until(session_id, lambda r, _out: r["status"] != "running")
        self.assertIn("done", output)
        self.assertEqual(final["exit_code"], 0)

        from agent_runtime.session import _get_session

        session = _get_session(session_id)
        with self.assertRaises(OSError):
            os.fstat(session.master_fd)

    def test_natural_exit_retention_is_bounded_and_eviction_is_truthful(self) -> None:
        from agent_runtime.session import TerminalSessionManager

        retention_bound = 16
        now = [100.0]
        manager = TerminalSessionManager(clock=lambda: now[0], start_reaper=False)
        self.addCleanup(manager.shutdown)
        session_ids: list[str] = []

        for index in range(retention_bound + 4):
            result = manager.start(
                [sys.executable, "-u", "-c", f"print('exit-{index}', flush=True)"],
                str(self.cwd),
            )
            session_id = str(result["session_id"])
            session_ids.append(session_id)
            deadline = time.monotonic() + 3.0
            while True:
                final = manager.poll(session_id, cursor=0, wait_ms=100)
                if final["status"] != "running":
                    break
                if time.monotonic() >= deadline:
                    self.fail(f"natural exit timeout for {session_id}")
            self.assertEqual(final["exit_code"], 0)

        self.assertEqual(
            sum(session.status == "running" for session in manager._sessions.values()),
            0,
        )
        retained_count = len(manager._sessions)

        for session_id in session_ids:
            for _ in range(3):
                try:
                    manager.poll(session_id, cursor=0, wait_ms=0)
                except ValueError as exc:
                    self.assertRegex(str(exc), "unknown|expired")

        self.assertLessEqual(retained_count, retention_bound)
        self.assertEqual(manager.poll(session_ids[-1], cursor=0, wait_ms=0)["status"], "exited")
        with self.assertRaisesRegex(ValueError, "unknown|expired"):
            manager.poll(session_ids[0], cursor=0, wait_ms=0)

    def test_operator_configured_concurrent_starts_respect_configured_capacity(self) -> None:
        from agent_runtime.session import TerminalSessionManager

        from agent_runtime.capacity import HeavyExecutionAdmission

        manager = TerminalSessionManager(
            max_active_sessions=4, admission=HeavyExecutionAdmission(4), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        barrier = threading.Barrier(5)
        results: list[str] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                result = manager.start(
                    [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
                    str(self.cwd),
                )
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)
            else:
                session_id = str(result["session_id"])
                with result_lock:
                    results.append(session_id)
                    self.session_ids.append(session_id)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3.0)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 4)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_explicit_operator_capacity_is_validated_and_enforced(self) -> None:
        from agent_runtime.session import TerminalSessionManager

        from agent_runtime.capacity import HeavyExecutionAdmission

        manager = TerminalSessionManager(
            max_active_sessions=3, admission=HeavyExecutionAdmission(3), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        sessions = [
            manager.start(
                [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
                str(self.cwd),
            )
            for _ in range(3)
        ]
        with self.assertRaisesRegex(RuntimeError, "three|3|maximum"):
            manager.start(
                [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
                str(self.cwd),
            )
        for result in sessions:
            manager.control(str(result["session_id"]), "terminate")

    def test_configured_capacity_supports_six_persistent_sessions(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            max_active_sessions=6, admission=HeavyExecutionAdmission(6), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        sessions = [
            manager.start(
                [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
                str(self.cwd),
            )
            for _ in range(6)
        ]
        self.assertEqual(len(sessions), 6)
        self.assertEqual(
            sum(session.status == "running" for session in manager._sessions.values()),
            6,
        )
        for result in sessions:
            manager.control(str(result["session_id"]), "terminate")

    def test_six_concurrent_terminal_start_calls_fill_configured_capacity(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            max_active_sessions=6, admission=HeavyExecutionAdmission(6), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        barrier = threading.Barrier(6)

        def launch(index: int) -> dict[str, object]:
            barrier.wait()
            return manager.start(
                [sys.executable, "-u", "-c", "import time; time.sleep(5)", str(index)],
                str(self.cwd),
            )

        with ThreadPoolExecutor(max_workers=6) as executor:
            sessions = list(executor.map(launch, range(6)))
        self.session_ids.extend(str(result["session_id"]) for result in sessions)
        self.assertEqual(len(sessions), 6)
        self.assertEqual(
            sum(session.status == "running" for session in manager._sessions.values()),
            6,
        )

    def test_concurrent_same_key_same_spec_spawns_exactly_once(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            max_active_sessions=6, admission=HeavyExecutionAdmission(6), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "a" * 32
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        barrier = threading.Barrier(8)
        call_count = 0
        call_lock = threading.Lock()
        real_popen = subprocess.Popen

        def counted_popen(*args, **kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
            return real_popen(*args, **kwargs)

        def launch() -> dict[str, object]:
            barrier.wait()
            return manager.start(argv, str(self.cwd), start_identity)

        with patch("agent_runtime.session.subprocess.Popen", side_effect=counted_popen):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _index: launch(), range(8)))

        self.assertEqual(call_count, 1)
        self.assertEqual({str(result["session_id"]) for result in results}, {str(results[0]["session_id"])})
        self.assertEqual({result["start_identity"] for result in results}, {start_identity})
        manager.control(str(results[0]["session_id"]), "terminate")

    def test_same_key_different_spec_conflicts_without_second_spawn(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            max_active_sessions=2, admission=HeavyExecutionAdmission(2), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "b" * 32
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        other_cwd = self.root / "other"
        other_cwd.mkdir()
        real_popen = subprocess.Popen
        calls = 0

        def counted_popen(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_popen(*args, **kwargs)

        with patch("agent_runtime.session.subprocess.Popen", side_effect=counted_popen):
            first = manager.start(argv, str(self.cwd), start_identity)
            with self.assertRaisesRegex(RuntimeError, "START_IDENTITY_CONFLICT"):
                manager.start(argv + ["different"], str(self.cwd), start_identity)
            with self.assertRaisesRegex(RuntimeError, "START_IDENTITY_CONFLICT"):
                manager.start(argv, str(other_cwd), start_identity)

        self.assertEqual(calls, 1)
        manager.control(str(first["session_id"]), "terminate")

    def test_concurrent_same_key_different_spec_collision_spawns_once(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            max_active_sessions=2, admission=HeavyExecutionAdmission(2), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "9" * 32
        barrier = threading.Barrier(2)
        real_popen = subprocess.Popen
        popen_calls = 0
        call_lock = threading.Lock()

        def counted_popen(*args, **kwargs):
            nonlocal popen_calls
            with call_lock:
                popen_calls += 1
            return real_popen(*args, **kwargs)

        def launch(label: str):
            barrier.wait()
            try:
                return ("ok", manager.start(
                    [sys.executable, "-u", "-c", "import time; time.sleep(30)", label],
                    str(self.cwd),
                    start_identity,
                ))
            except BaseException as exc:
                return ("error", exc)

        with patch("agent_runtime.session.subprocess.Popen", side_effect=counted_popen):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(launch, ("left", "right")))

        successes = [value for kind, value in outcomes if kind == "ok"]
        failures = [value for kind, value in outcomes if kind == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertRegex(str(failures[0]), "START_IDENTITY_CONFLICT")
        self.assertEqual(popen_calls, 1)
        manager.control(str(successes[0]["session_id"]), "terminate")

    def test_keyed_failure_immediately_before_popen_is_pre_effect_and_retained(self) -> None:
        import agent_runtime.session as session

        from agent_runtime.capacity import HeavyExecutionAdmission

        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "8" * 32
        argv = [sys.executable, "-c", "print('must-not-run')"]

        with patch.object(session.pty, "openpty", side_effect=OSError("injected pre-popen failure")), patch.object(
            session.subprocess, "Popen"
        ) as popen:
            with self.assertRaisesRegex(OSError, "pre-popen"):
                manager.start(argv, str(self.cwd), start_identity)
            popen.assert_not_called()

        retained = manager.poll(start_identity=start_identity)
        self.assertEqual(retained["lifecycle"], "START_FAILED_PRE_EFFECT")
        self.assertEqual(retained["termination_reason"], "start_failed_pre_effect")

    def test_keyed_overflow_before_recovery_poll_reports_truthful_cursor_loss(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import MAX_POLL_OUTPUT_BYTES, MAX_RETAINED_OUTPUT_BYTES

        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "7" * 32
        size = MAX_RETAINED_OUTPUT_BYTES + MAX_POLL_OUTPUT_BYTES + 8192
        started = manager.start(
            [
                sys.executable,
                "-u",
                "-c",
                f"import sys,time; sys.stdout.write('x'*{size}); sys.stdout.flush(); time.sleep(1)",
            ],
            str(self.cwd),
            start_identity,
        )

        time.sleep(0.25)
        recovered = manager.poll(start_identity=start_identity, cursor=0, wait_ms=0)
        self.assertEqual(recovered["session_id"], started["session_id"])
        self.assertTrue(recovered["cursor_expired"])
        self.assertGreater(recovered["dropped_output_bytes"], 0)
        self.assertLessEqual(len(recovered["output"].encode()), MAX_POLL_OUTPUT_BYTES)
        manager.control(str(started["session_id"]), "terminate")

    def test_pre_effect_failure_is_retained_and_same_key_never_respawns(self) -> None:
        from unittest import mock

        from agent_runtime.session import TerminalSessionManager

        admission = mock.Mock()
        admission.acquire.side_effect = RuntimeError("injected admission failure")
        manager = TerminalSessionManager(admission=admission, start_reaper=False)
        self.addCleanup(manager.shutdown)
        start_identity = "c" * 32
        argv = [sys.executable, "-u", "-c", "print('must-not-run')"]

        with patch("agent_runtime.session.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "injected admission failure"):
                manager.start(argv, str(self.cwd), start_identity)
            popen.assert_not_called()
            retained = manager.poll(start_identity=start_identity)
            retry = manager.start(argv, str(self.cwd), start_identity)
            popen.assert_not_called()

        self.assertEqual(retained["lifecycle"], "START_FAILED_PRE_EFFECT")
        self.assertEqual(retained["termination_reason"], "start_failed_pre_effect")
        self.assertEqual(retry["session_id"], retained["session_id"])

    def test_post_effect_failure_remains_observable_and_same_key_never_respawns(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "d" * 32
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        real_popen = subprocess.Popen
        popen_calls = 0

        def counted_popen(*args, **kwargs):
            nonlocal popen_calls
            popen_calls += 1
            return real_popen(*args, **kwargs)

        with patch("agent_runtime.session.subprocess.Popen", side_effect=counted_popen), patch(
            "agent_runtime.session.threading.Thread.start",
            side_effect=RuntimeError("injected post-popen setup failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "post-popen"):
                manager.start(argv, str(self.cwd), start_identity)

        retained = manager.poll(start_identity=start_identity)
        self.assertEqual(retained["lifecycle"], "START_FAILED_POST_EFFECT")
        self.assertEqual(retained["termination_reason"], "start_failed_post_effect")
        retry = manager.start(argv, str(self.cwd), start_identity)
        self.assertEqual(retry["session_id"], retained["session_id"])
        self.assertEqual(popen_calls, 1)

    def test_lost_start_ack_reconciles_by_identity_to_exact_same_operation(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        start_identity = "e" * 32
        argv = [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"]
        started = manager.start(argv, str(self.cwd), start_identity)

        recovered = manager.poll(start_identity=start_identity, cursor=0, wait_ms=100)
        self.assertEqual(recovered["session_id"], started["session_id"])
        self.assertEqual(recovered["start_identity"], start_identity)
        self.assertIn(recovered["lifecycle"], {"RUNNING", "COMPLETED"})
        manager.control(str(started["session_id"]), "terminate")

    def test_full_capacity_reconciles_existing_key_and_rejects_new_key_without_spawn(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(
            max_active_sessions=6, admission=HeavyExecutionAdmission(6), start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        sessions = [
            manager.start(argv + [str(index)], str(self.cwd), f"{index:032x}")
            for index in range(6)
        ]

        with patch("agent_runtime.session.subprocess.Popen") as popen:
            reconciled = manager.start(argv + ["0"], str(self.cwd), f"{0:032x}")
            with self.assertRaisesRegex(RuntimeError, "maximum"):
                manager.start(argv + ["new"], str(self.cwd), "f" * 32)
            popen.assert_not_called()

        self.assertEqual(reconciled["session_id"], sessions[0]["session_id"])
        rejected = manager.poll(start_identity="f" * 32)
        self.assertEqual(rejected["lifecycle"], "START_FAILED_PRE_EFFECT")
        for result in sessions:
            manager.control(str(result["session_id"]), "terminate")

    def test_completed_retention_is_time_bounded_and_poll_does_not_refresh_it(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import COMPLETED_RETENTION_SECONDS, TerminalSessionManager

        self.assertEqual(COMPLETED_RETENTION_SECONDS, 3600.0)
        now = [100.0]
        manager = TerminalSessionManager(
            clock=lambda: now[0],
            admission=HeavyExecutionAdmission(1),
            start_reaper=False,
        )
        self.addCleanup(manager.shutdown)
        start_identity = "1" * 32
        result = manager.start(
            [sys.executable, "-u", "-c", "print('done', flush=True)"],
            str(self.cwd),
            start_identity,
        )
        session_id = str(result["session_id"])
        deadline = time.monotonic() + 3.0
        while manager.poll(start_identity=start_identity, wait_ms=100)["status"] == "running":
            if time.monotonic() >= deadline:
                self.fail("session did not complete")

        now[0] += 3599.0
        self.assertEqual(manager.poll(start_identity=start_identity)["session_id"], session_id)
        now[0] += 2.0
        manager.reap_once()
        with self.assertRaisesRegex(ValueError, "START_IDENTITY_UNKNOWN"):
            manager.poll(start_identity=start_identity)

    def test_explicit_terminate_is_retained_and_restart_loses_identity_without_respawn(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        start_identity = "2" * 32
        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1), start_reaper=False
        )
        result = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
            str(self.cwd),
            start_identity,
        )
        manager.control(str(result["session_id"]), "terminate")
        retained = manager.poll(start_identity=start_identity)
        self.assertEqual(retained["termination_reason"], "explicit_terminate")
        manager.shutdown()

        replacement = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1), start_reaper=False
        )
        self.addCleanup(replacement.shutdown)
        with patch("agent_runtime.session.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "START_IDENTITY_UNKNOWN"):
                replacement.poll(start_identity=start_identity)
            popen.assert_not_called()

    def test_start_identity_and_poll_selector_validation_fail_closed(self) -> None:
        from agent_runtime.session import TerminalSessionManager

        manager = TerminalSessionManager(start_reaper=False)
        self.addCleanup(manager.shutdown)
        argv = [sys.executable, "-c", "pass"]
        for invalid in ("", "a" * 31, "a" * 33, "A" * 32, "g" * 32):
            with self.subTest(start_identity=invalid):
                with self.assertRaisesRegex(ValueError, "32 lowercase"):
                    manager.start(argv, str(self.cwd), invalid)

        with self.assertRaisesRegex(ValueError, "exactly one"):
            manager.poll()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            manager.poll(session_id="x", start_identity="3" * 32)

    def test_session_limit_setting_uses_positive_integer_and_safe_fallback(self) -> None:
        from agent_runtime.session import DEFAULT_SESSION_LIMIT, effective_session_limit

        self.assertEqual(effective_session_limit("6"), 6)
        self.assertEqual(effective_session_limit("1"), 1)
        self.assertEqual(effective_session_limit(""), DEFAULT_SESSION_LIMIT)
        self.assertEqual(effective_session_limit("invalid"), DEFAULT_SESSION_LIMIT)
        self.assertEqual(effective_session_limit("0"), DEFAULT_SESSION_LIMIT)
        self.assertEqual(effective_session_limit("-2"), DEFAULT_SESSION_LIMIT)
        self.assertEqual(effective_session_limit("7"), DEFAULT_SESSION_LIMIT)

    def test_no_polling_beyond_old_idle_ttl_does_not_kill_running_session(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import RUNNING_HARD_WALL_SECONDS, TerminalSessionManager

        self.assertEqual(RUNNING_HARD_WALL_SECONDS, 3600.0)
        now = [100.0]
        admission = HeavyExecutionAdmission(1)
        manager = TerminalSessionManager(
            clock=lambda: now[0],
            admission=admission,
            start_reaper=False,
        )
        self.addCleanup(manager.shutdown)
        result = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
            str(self.cwd),
        )
        session_id = str(result["session_id"])

        now[0] += 601.0
        self.assertEqual(manager.reap_once(), [])
        self.assertEqual(manager.poll(session_id, cursor=0, wait_ms=0)["status"], "running")
        self.assertEqual(admission.active, 1)

    def test_controlled_time_hard_wall_terminates_and_releases_capacity(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission
        from agent_runtime.session import TerminalSessionManager

        now = [100.0]
        admission = HeavyExecutionAdmission(1)
        manager = TerminalSessionManager(
            clock=lambda: now[0],
            admission=admission,
            start_reaper=False,
        )
        self.addCleanup(manager.shutdown)
        result = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
            str(self.cwd),
        )
        session_id = str(result["session_id"])
        now[0] += 3601.0
        self.assertIn(session_id, manager.reap_once())
        retained = manager.poll(session_id, cursor=0, wait_ms=0)
        self.assertEqual(retained["status"], "exited")
        self.assertEqual(retained["termination_reason"], "hard_wall_timeout")
        self.assertEqual(admission.active, 0)

    def test_control_write_and_finalization_have_one_lifecycle_order(self) -> None:
        import agent_runtime.session as session
        from unittest import mock

        manager = TerminalSessionManager(start_reaper=False)
        process = mock.Mock()
        process.poll.return_value = None
        process.returncode = 0
        fixture = session._Session(
            session_id="control-finalization-race",
            process=process,
            master_fd=123,
            cwd=str(self.cwd),
            argv=["/bin/zsh"],
            last_activity=100.0,
        )
        fixture.reader_done.set()
        manager._sessions[fixture.session_id] = fixture

        running_checked = threading.Event()
        release_control = threading.Event()
        cleanup_entered = threading.Event()
        fd_closed = threading.Event()
        write_after_close: list[bool] = []
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        original_require_running = manager._require_running

        def gated_require_running(candidate) -> None:
            original_require_running(candidate)
            running_checked.set()
            if not release_control.wait(timeout=2.0):
                raise AssertionError("control release was not signaled")

        def fake_terminate(_process) -> None:
            cleanup_entered.set()

        def fake_close(_fd: int) -> None:
            fd_closed.set()

        def fake_write(_fd: int, view) -> int:
            write_after_close.append(fd_closed.is_set())
            return len(view)

        def run_control() -> None:
            try:
                manager.control(fixture.session_id, "write", data="x")
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        def run_cleanup() -> None:
            try:
                manager._cleanup_process(fixture, "natural_exit")
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        with mock.patch.object(session._PROTECTED_GUARD, "check"), mock.patch.object(
            manager, "_require_running", side_effect=gated_require_running
        ), mock.patch.object(session, "_terminate_process_group", side_effect=fake_terminate), mock.patch.object(
            session.os, "close", side_effect=fake_close
        ), mock.patch.object(session.os, "write", side_effect=fake_write), mock.patch.object(
            session, "emit_process_end"
        ):
            control_thread = threading.Thread(target=run_control)
            cleanup_thread = threading.Thread(target=run_cleanup)
            control_thread.start()
            self.assertTrue(running_checked.wait(timeout=1.0))
            cleanup_thread.start()

            if cleanup_entered.wait(timeout=0.5):
                self.assertTrue(fd_closed.wait(timeout=1.0))
            release_control.set()

            control_thread.join(timeout=2.0)
            cleanup_thread.join(timeout=2.0)

        self.assertFalse(control_thread.is_alive(), "control thread deadlocked")
        self.assertFalse(cleanup_thread.is_alive(), "cleanup thread deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(write_after_close, [False])

    def test_polling_does_not_extend_running_hard_wall(self) -> None:
        from agent_runtime.capacity import HeavyExecutionAdmission

        now = [100.0]
        admission = HeavyExecutionAdmission(1)
        manager = TerminalSessionManager(
            clock=lambda: now[0],
            admission=admission,
            start_reaper=False,
        )
        self.addCleanup(manager.shutdown)
        result = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
            str(self.cwd),
        )
        session_id = str(result["session_id"])

        now[0] += 3500.0
        self.assertEqual(manager.poll(session_id, cursor=0, wait_ms=0)["status"], "running")
        now[0] += 101.0
        self.assertIn(session_id, manager.reap_once())
        final = manager.poll(session_id, cursor=0, wait_ms=0)
        self.assertEqual(final["termination_reason"], "hard_wall_timeout")
        self.assertEqual(admission.active, 0)

    def test_waiting_poll_does_not_block_explicit_terminate_or_deadlock(self) -> None:
        import agent_runtime.session as session
        from unittest import mock

        manager = TerminalSessionManager(start_reaper=False)
        process = mock.Mock()
        process.poll.return_value = None
        process.returncode = 0
        fixture = session._Session(
            session_id="poll-terminate-order",
            process=process,
            master_fd=123,
            cwd=str(self.cwd),
            argv=["/bin/zsh"],
            last_activity=100.0,
        )
        fixture.reader_done.set()
        wait_entered = threading.Event()
        terminate_done = threading.Event()
        errors: list[BaseException] = []

        class SignalingCondition(threading.Condition):
            def wait(self, timeout=None):
                wait_entered.set()
                return super().wait(timeout)

        fixture.changed = SignalingCondition(fixture.lock)
        manager._sessions[fixture.session_id] = fixture

        def run_poll() -> None:
            try:
                manager.poll(fixture.session_id, cursor=0, wait_ms=1000)
            except BaseException as exc:
                errors.append(exc)

        def run_terminate() -> None:
            try:
                manager.control(fixture.session_id, "terminate")
            except BaseException as exc:
                errors.append(exc)
            finally:
                terminate_done.set()

        with mock.patch.object(session, "_terminate_process_group"), mock.patch.object(
            session.os, "close"
        ), mock.patch.object(session, "emit_process_end"):
            poll_thread = threading.Thread(target=run_poll)
            terminate_thread = threading.Thread(target=run_terminate)
            poll_thread.start()
            self.assertTrue(wait_entered.wait(timeout=1.0))
            terminate_thread.start()
            self.assertTrue(
                terminate_done.wait(timeout=0.75),
                "terminate was blocked by poll wait or self-deadlocked",
            )
            terminate_thread.join(timeout=1.0)
            poll_thread.join(timeout=1.0)

        self.assertFalse(terminate_thread.is_alive(), "terminate thread deadlocked")
        self.assertFalse(poll_thread.is_alive(), "poll thread did not wake after cleanup")
        self.assertEqual(errors, [])

    def test_natural_exit_and_shutdown_kill_descendants_and_runtime_creates_no_session_files(self) -> None:
        from agent_runtime.session import TerminalSessionManager

        marker_natural = self.cwd / "natural-descendant-survived"
        marker_shutdown = self.cwd / "shutdown-descendant-survived"
        initial_files = {p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file()}

        natural_code = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',"
            + repr(
                f"import time,pathlib; time.sleep(.5); pathlib.Path({str(marker_natural)!r}).write_text('alive')"
            )
            + "])"
        )
        result = self.start([sys.executable, "-u", "-c", natural_code])
        self.poll_until(str(result["session_id"]), lambda r, _out: r["status"] != "running")
        time.sleep(0.7)
        self.assertFalse(marker_natural.exists())

        manager = TerminalSessionManager(start_reaper=False)
        shutdown_code = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',"
            + repr(
                f"import time,pathlib; time.sleep(.5); pathlib.Path({str(marker_shutdown)!r}).write_text('alive')"
            )
            + "]); time.sleep(10)"
        )
        managed = manager.start([sys.executable, "-u", "-c", shutdown_code], str(self.cwd))
        manager.shutdown()
        self.assertFalse(manager.has_session(str(managed["session_id"])))
        time.sleep(0.7)
        self.assertFalse(marker_shutdown.exists())
        final_files = {p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(final_files, initial_files)

    def test_runtime_signals_cleanup_persistent_descendant_before_default_exit(self) -> None:
        runtime_root = Path(__file__).resolve().parents[1]

        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal.Signals(signum).name):
                case_root = self.root / signal.Signals(signum).name
                case_root.mkdir()
                ready = case_root / "runtime-ready"
                descendant_pid = case_root / "descendant-pid"
                session_pgid = case_root / "session-pgid"
                marker = case_root / "descendant-survived"
                descendant_code = (
                    "import pathlib,time; "
                    "time.sleep(1.0); "
                    f"pathlib.Path({str(marker)!r}).write_text('alive')"
                )
                session_code = (
                    "import os,pathlib,subprocess,sys,time; "
                    f"descendant=subprocess.Popen([sys.executable,'-c',{descendant_code!r}]); "
                    f"pathlib.Path({str(descendant_pid)!r}).write_text(str(descendant.pid)); "
                    f"pathlib.Path({str(session_pgid)!r}).write_text(str(os.getpid())); "
                    "time.sleep(30)"
                )
                runtime_code = f"""
import pathlib, sys, time, types

class FakeMCPServer:
    def __init__(self, name=None, **_metadata):
        self.middleware = []

    def tool(self, annotations=None):
        def decorate(function):
            return function
        return decorate

    def run(self):
        ready = pathlib.Path({str(ready)!r})
        descendant_pid = pathlib.Path({str(descendant_pid)!r})
        deadline = time.monotonic() + 5
        while not descendant_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        ready.write_text('ready')
        while True:
            time.sleep(1)

mcp_package = types.ModuleType('mcp')
mcp_package.__path__ = []
mcp_server = types.ModuleType('mcp.server')
mcp_server.__path__ = []
mcp_server.MCPServer = FakeMCPServer
mcp_mcpserver = types.ModuleType('mcp.server.mcpserver')
mcp_mcpserver.__path__ = []
mcp_types = types.ModuleType('mcp.types')
class FakeCallToolResult:
    pass
class FakeImageContent:
    pass
class FakeTextContent:
    pass
mcp_types.CallToolResult = FakeCallToolResult
mcp_types.ImageContent = FakeImageContent
mcp_types.TextContent = FakeTextContent
mcp_exceptions = types.ModuleType('mcp.server.mcpserver.exceptions')
class FakeToolError(Exception):
    pass
mcp_exceptions.ToolError = FakeToolError
sys.modules['mcp'] = mcp_package
sys.modules['mcp.server'] = mcp_server
sys.modules['mcp.server.mcpserver'] = mcp_mcpserver
sys.modules['mcp.server.mcpserver.exceptions'] = mcp_exceptions
sys.modules['mcp.types'] = mcp_types

from agent_runtime import server
from agent_runtime.session import start_terminal

start_terminal(
    [sys.executable, '-u', '-c', {session_code!r}],
    {str(self.cwd)!r},
)
server._main()
"""
                env = os.environ.copy()
                env["PYTHONPATH"] = os.pathsep.join(
                    value for value in (str(runtime_root), env.get("PYTHONPATH", "")) if value
                )
                process = subprocess.Popen(
                    [sys.executable, "-u", "-c", runtime_code],
                    cwd=str(runtime_root),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            break
                        time.sleep(0.01)
                    if not ready.exists():
                        stderr = process.communicate(timeout=1)[1]
                        self.fail(
                            f"runtime did not become ready for {signal.Signals(signum).name}: "
                            f"returncode={process.returncode!r} stderr={stderr!r}"
                        )

                    process.send_signal(signum)
                    returncode = process.wait(timeout=5)
                    assert process.stderr is not None
                    process.stderr.close()
                    time.sleep(1.2)
                    self.assertEqual(returncode, -signum)
                    self.assertFalse(marker.exists())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    if process.stderr is not None and not process.stderr.closed:
                        process.stderr.close()
                    if session_pgid.exists():
                        try:
                            pgid = int(session_pgid.read_text())
                            if os.getpgid(pgid) == pgid:
                                os.killpg(pgid, signal.SIGKILL)
                        except (ProcessLookupError, ValueError):
                            pass

    def test_natural_exit_emits_one_persistent_process_event(self) -> None:
        output = io.StringIO()
        context, token = bind_call_context(31)
        try:
            with patch("agent_runtime.timing.sys.stderr", output):
                result = self.start([sys.executable, "-u", "-c", "print('ready')"])
                final, _ = self.poll_until(
                    str(result["session_id"]), lambda r, _out: r["status"] != "running"
                )
        finally:
            reset_call_context(token)

        self.assertEqual(final["exit_code"], 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        process_events = [event for event in events if event["event_kind"] == "process_end"]
        self.assertEqual(len(process_events), 1)
        event = process_events[0]
        self.assertEqual(event["runtime_call_id"], context.runtime_call_id)
        self.assertEqual(event["raw_request_id"], 31)
        self.assertEqual(event["request_id_type"], "int")
        self.assertEqual(event["tool_name"], "terminal_start")
        self.assertEqual(event["process_kind"], "persistent_pty")
        self.assertEqual(event["termination_state"], "natural_exit")

    def test_explicit_terminate_emits_only_one_persistent_process_event(self) -> None:
        output = io.StringIO()
        context, token = bind_call_context(32)
        try:
            with patch("agent_runtime.timing.sys.stderr", output):
                result = self.start([sys.executable, "-u", "-c", "import time; time.sleep(5)"])
                control_terminal(str(result["session_id"]), "terminate")
        finally:
            reset_call_context(token)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        process_events = [event for event in events if event["event_kind"] == "process_end"]
        self.assertEqual(len(process_events), 1)
        self.assertEqual(process_events[0]["runtime_call_id"], context.runtime_call_id)
        self.assertEqual(process_events[0]["termination_state"], "explicit_terminate")

    def test_hard_wall_and_shutdown_emit_bounded_persistent_process_events(self) -> None:
        output = io.StringIO()
        now = [100.0]
        manager = TerminalSessionManager(clock=lambda: now[0], start_reaper=False)
        self.addCleanup(manager.shutdown)

        context, token = bind_call_context(33)
        try:
            with patch("agent_runtime.timing.sys.stderr", output):
                managed = manager.start(
                    [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                    str(self.cwd),
                )
                now[0] += 3601.0
                self.assertIn(str(managed["session_id"]), manager.reap_once())

                manager.start(
                    [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                    str(self.cwd),
                )
                manager.shutdown()
        finally:
            reset_call_context(token)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        process_events = [event for event in events if event["event_kind"] == "process_end"]
        self.assertEqual(len(process_events), 2)
        self.assertEqual({event["runtime_call_id"] for event in process_events}, {context.runtime_call_id})
        self.assertEqual(
            {event["termination_state"] for event in process_events},
            {"hard_wall_timeout", "shutdown"},
        )


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
