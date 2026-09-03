from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.session import control_terminal, poll_terminal, start_terminal


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
        time.sleep(0.2)
        polled = poll_terminal(session_id, cursor=0, wait_ms=0)
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

    def test_concurrent_starts_cannot_exceed_three_active_sessions(self) -> None:
        barrier = threading.Barrier(4)
        results: list[str] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                result = start_terminal(
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

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3.0)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_maximum_three_active_sessions_and_fourth_is_rejected(self) -> None:
        sessions = [
            self.start([sys.executable, "-u", "-c", "import time; time.sleep(5)"])
            for _ in range(3)
        ]
        with self.assertRaisesRegex(RuntimeError, "three|3|maximum"):
            start_terminal([sys.executable, "-u", "-c", "import time; time.sleep(5)"], str(self.cwd))
        for result in sessions:
            control_terminal(str(result["session_id"]), "terminate")

    def test_idle_ttl_is_fixed_and_reaper_expires_without_a_follow_up_call(self) -> None:
        from agent_runtime.session import IDLE_TTL_SECONDS, TerminalSessionManager

        self.assertEqual(IDLE_TTL_SECONDS, 600.0)
        manager = TerminalSessionManager(idle_ttl_seconds=0.05, reaper_interval=0.01)
        self.addCleanup(manager.shutdown)
        result = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
            str(self.cwd),
        )
        session_id = str(result["session_id"])
        time.sleep(0.2)
        with self.assertRaisesRegex(ValueError, "unknown|expired"):
            manager.poll(session_id)
        self.assertFalse(manager.has_session(session_id))

    def test_controlled_time_idle_reap_terminates_process_group(self) -> None:
        from agent_runtime.session import TerminalSessionManager

        now = [100.0]
        manager = TerminalSessionManager(clock=lambda: now[0], start_reaper=False)
        self.addCleanup(manager.shutdown)
        result = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(5)"],
            str(self.cwd),
        )
        session_id = str(result["session_id"])
        now[0] += 601.0
        expired = manager.reap_idle_once()
        self.assertEqual(expired, [session_id])
        self.assertFalse(manager.has_session(session_id))

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


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
