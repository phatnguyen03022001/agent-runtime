from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_runtime.capacity import HeavyExecutionAdmission
from agent_runtime.errors import RuntimeStateError
from agent_runtime.session import TerminalSessionManager, _BoundedPipeCapture
from agent_runtime.tool_contract import EffectState


class UnifiedLifecycleTests(unittest.TestCase):
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
        self.manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(6), start_reaper=False
        )

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_pipe_requires_identity_and_rejects_input_and_resize(self) -> None:
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        with patch("agent_runtime.session.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "requires start_identity"):
                self.manager.start(argv, str(self.cwd), mode="pipe")
            popen.assert_not_called()

        started = self.manager.start(argv, str(self.cwd), "a" * 32, mode="pipe")
        session_id = str(started["session_id"])
        with self.assertRaisesRegex(ValueError, "do not accept input"):
            self.manager.control(session_id, "write", data="x")
        with self.assertRaisesRegex(ValueError, "only for PTY"):
            self.manager.control(session_id, "resize", rows=24, cols=80)
        self.manager.control(session_id, "terminate")

    def test_pipe_uses_closed_stdin_and_preserves_stream_identity(self) -> None:
        start_identity = "b" * 32
        argv = [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write('out-λ\\n'.encode()); "
                "sys.stderr.buffer.write('err-Ω\\n'.encode()); "
                "sys.stdout.flush(); sys.stderr.flush(); "
                "assert sys.stdin.read() == ''"
            ),
        ]
        started = self.manager.start(argv, str(self.cwd), start_identity, mode="pipe")
        self.assertEqual(started["mode"], "pipe")
        self.assertEqual(started["output"], "")
        self.assertIn("output_chunks", started)

        chunks: list[dict[str, str]] = []
        cursor = 0
        final = started
        for _ in range(30):
            final = self.manager.poll(
                session_id=str(started["session_id"]),
                cursor=cursor,
                wait_ms=100,
                wait_for="terminal_or_deadline",
            )
            chunks.extend(final["output_chunks"])
            cursor = int(final["next_cursor"])
            if final["status"] == "exited":
                break
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual({chunk["stream"] for chunk in chunks}, {"stdout", "stderr"})
        self.assertIn("out-λ", "".join(c["text"] for c in chunks if c["stream"] == "stdout"))
        self.assertIn("err-Ω", "".join(c["text"] for c in chunks if c["stream"] == "stderr"))
        self.assertLessEqual(cursor, 16 * 1024)

    def test_terminal_exec_concurrent_duplicate_and_fast_retry_share_one_result(self) -> None:
        start_identity = "c" * 32
        argv = [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys,time; time.sleep(.1); "
                "print('stdout-λ'); print('stderr-Ω', file=sys.stderr)"
            ),
        ]
        real_popen = subprocess.Popen
        popen_count = 0
        popen_lock = threading.Lock()
        popen_entered = threading.Event()
        allow_popen = threading.Event()
        second_start_entered = threading.Event()
        start_count = 0
        start_lock = threading.Lock()
        real_start = self.manager.start

        def counted_popen(*args, **kwargs):
            nonlocal popen_count
            with popen_lock:
                popen_count += 1
            popen_entered.set()
            if not allow_popen.wait(3):
                raise TimeoutError("test did not release process dispatch")
            return real_popen(*args, **kwargs)

        def counted_start(*args, **kwargs):
            nonlocal start_count
            with start_lock:
                start_count += 1
                if start_count == 2:
                    second_start_entered.set()
            return real_start(*args, **kwargs)

        self.manager.start = counted_start  # type: ignore[method-assign]
        def execute():
            return self.manager.execute(argv, str(self.cwd), start_identity, 2.0)

        try:
            with patch("agent_runtime.session.subprocess.Popen", side_effect=counted_popen):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(execute)
                    second = pool.submit(execute)
                    self.assertTrue(popen_entered.wait(2))
                    self.assertTrue(second_start_entered.wait(2))
                    allow_popen.set()
                    first_result = first.result(timeout=5)
                    second_result = second.result(timeout=5)
        finally:
            allow_popen.set()
            self.manager.start = real_start  # type: ignore[method-assign]

        self.assertEqual(popen_count, 1)
        self.assertEqual(first_result, second_result)
        self.assertEqual(first_result["start_identity"], start_identity)
        self.assertEqual(first_result["stdout"].strip(), "stdout-λ")
        self.assertEqual(first_result["stderr"].strip(), "stderr-Ω")
        duplicate = self.manager.execute(argv, str(self.cwd), start_identity, 2.0)
        self.assertEqual(duplicate, first_result)
        self.assertEqual(popen_count, 1)
        with self.assertRaisesRegex(RuntimeError, "START_IDENTITY_CONFLICT"):
            self.manager.execute(argv, str(self.cwd), start_identity, 3.0)

    def test_key_is_pollable_before_popen_and_repeated_start_does_not_dispatch(self) -> None:
        start_identity = "d" * 32
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        real_popen = subprocess.Popen
        entered = threading.Event()
        release = threading.Event()
        popen_count = 0

        def gated_popen(*args, **kwargs):
            nonlocal popen_count
            popen_count += 1
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release process dispatch")
            return real_popen(*args, **kwargs)

        with patch("agent_runtime.session.subprocess.Popen", side_effect=gated_popen):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    self.manager.start, argv, str(self.cwd), start_identity, "pipe"
                )
                self.assertTrue(entered.wait(2))
                observed = self.manager.poll(start_identity=start_identity)
                self.assertEqual(observed["status"], "starting")
                repeated = self.manager.start(argv, str(self.cwd), start_identity, "pipe")
                self.assertEqual(repeated["session_id"], observed["session_id"])
                self.assertEqual(repeated["status"], "starting")
                self.assertEqual(popen_count, 1)
                release.set()
                started = pending.result(timeout=5)
        self.assertEqual(started["session_id"], observed["session_id"])
        self.manager.control(str(started["session_id"]), "terminate")

    def test_identity_conflicts_across_mode_and_entry_surface_before_second_spawn(self) -> None:
        start_identity = "e" * 32
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        real_popen = subprocess.Popen
        popen_count = 0

        def counted_popen(*args, **kwargs):
            nonlocal popen_count
            popen_count += 1
            return real_popen(*args, **kwargs)

        with patch("agent_runtime.session.subprocess.Popen", side_effect=counted_popen):
            started = self.manager.start(argv, str(self.cwd), start_identity, mode="pipe")
            with self.assertRaisesRegex(RuntimeError, "START_IDENTITY_CONFLICT"):
                self.manager.start(argv, str(self.cwd), start_identity, mode="pty")
            with self.assertRaisesRegex(RuntimeError, "START_IDENTITY_CONFLICT"):
                self.manager.execute(argv, str(self.cwd), start_identity, 2.0)
        self.assertEqual(popen_count, 1)
        self.manager.control(str(started["session_id"]), "terminate")


    def test_terminal_exec_capture_unavailable_is_unknown_after_dispatch(self) -> None:
        start_identity = "f" * 32
        with patch("agent_runtime.session._BoundedPipeCapture", side_effect=[None, None]):
            with self.assertRaises(RuntimeStateError) as raised:
                self.manager.execute(
                    [sys.executable, "-u", "-c", "print('dispatched')"],
                    str(self.cwd),
                    start_identity,
                    2.0,
                )
        self.assertEqual(raised.exception.effect_state, EffectState.UNKNOWN)
        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(raised.exception.safe_next_action, "reconcile")
        observed = self.manager.poll(start_identity=start_identity)
        self.assertEqual(observed["status"], "exited")
        self.assertEqual(observed["exit_code"], 0)

    def test_terminal_exec_missing_exit_code_is_unknown_after_dispatch(self) -> None:
        start_identity = "0" * 32
        completed_processes: list[tuple[subprocess.Popen[bytes], int | None]] = []

        def discard_exit_code(process):
            process.wait(timeout=2)
            completed_processes.append((process, process.returncode))
            process.returncode = None

        with patch("agent_runtime.session._terminate_process_group", side_effect=discard_exit_code):
            with self.assertRaises(RuntimeStateError) as raised:
                self.manager.execute(
                    [sys.executable, "-u", "-c", "print('dispatched')"],
                    str(self.cwd),
                    start_identity,
                    2.0,
                )
        self.assertEqual(raised.exception.effect_state, EffectState.UNKNOWN)
        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(raised.exception.reason_code, "PROCESS_COMPLETION_UNKNOWN")
        process, actual_exit_code = completed_processes[0]
        process.returncode = actual_exit_code

    def test_pipe_reader_failure_terminates_and_terminal_exec_reports_unknown(self) -> None:
        start_identity = "9" * 32
        real_popen = subprocess.Popen
        real_read = os.read
        entered = threading.Event()
        release = threading.Event()
        broken_fd: int | None = None

        def gated_popen(*args, **kwargs):
            nonlocal broken_fd
            process = real_popen(*args, **kwargs)
            assert process.stdout is not None
            broken_fd = process.stdout.fileno()
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release process dispatch")
            return process

        def failing_read(fd: int, size: int) -> bytes:
            if fd == broken_fd:
                raise OSError(errno.EIO, "injected pipe reader failure")
            return real_read(fd, size)

        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        try:
            with patch("agent_runtime.session.subprocess.Popen", side_effect=gated_popen):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    operation = pool.submit(
                        self.manager.execute, argv, str(self.cwd), start_identity, 5.0
                    )
                    self.assertTrue(entered.wait(2))
                    with patch("agent_runtime.session.os.read", side_effect=failing_read):
                        release.set()
                        with self.assertRaises(RuntimeStateError) as raised:
                            operation.result(timeout=5)
        finally:
            release.set()

        self.assertEqual(raised.exception.effect_state, EffectState.UNKNOWN)
        self.assertTrue(raised.exception.reconciliation_required)
        observed = self.manager.poll(start_identity=start_identity)
        self.assertEqual(observed["status"], "exited")
        self.assertEqual(observed["lifecycle"], "START_FAILED_POST_EFFECT")
        self.assertEqual(observed["termination_reason"], "start_failed_post_effect")

    def test_terminal_exec_reports_unknown_when_pipe_reader_cannot_drain(self) -> None:
        start_identity = "7" * 32
        entered = threading.Event()
        release = threading.Event()
        original_consume = _BoundedPipeCapture.consume

        def blocked_consume(capture, chunk: bytes) -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release delayed capture reader")
            original_consume(capture, chunk)

        try:
            with patch(
                "agent_runtime.session._BoundedPipeCapture.consume",
                new=blocked_consume,
            ):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    operation = pool.submit(
                        self.manager.execute,
                        [sys.executable, "-u", "-c", "print('late-output')"],
                        str(self.cwd),
                        start_identity,
                        2.0,
                    )
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(RuntimeStateError) as raised:
                        operation.result(timeout=2)
                    self.assertEqual(raised.exception.effect_state, EffectState.UNKNOWN)
                    self.assertTrue(raised.exception.reconciliation_required)
                    observed = self.manager.poll(start_identity=start_identity)
                    self.assertEqual(observed["status"], "exited")
                    self.assertEqual(observed["lifecycle"], "START_FAILED_POST_EFFECT")
                    release.set()
            after_drain = self.manager.poll(start_identity=start_identity)
            self.assertEqual(after_drain["output_chunks"], [])
        finally:
            release.set()

    def test_cleanup_failure_is_unknown_and_keeps_key_pollable(self) -> None:
        start_identity = "8" * 32
        argv = [sys.executable, "-u", "-c", "import time; time.sleep(30)"]
        with patch(
            "agent_runtime.session._terminate_process_group",
            side_effect=PermissionError("injected cleanup failure"),
        ):
            self.manager.start(
                argv,
                str(self.cwd),
                start_identity,
                mode="pipe",
                entry_surface="terminal_exec",
                timeout_seconds=1.0,
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                observed = self.manager.poll(start_identity=start_identity, wait_ms=100)
                if observed["lifecycle"] == "START_FAILED_POST_EFFECT":
                    break
            else:
                self.fail("post-effect cleanup failure was not observable by key")

            with self.assertRaises(RuntimeStateError) as raised:
                self.manager.execute(argv, str(self.cwd), start_identity, 1.0)

        self.assertEqual(raised.exception.effect_state, EffectState.UNKNOWN)
        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(observed["lifecycle"], "START_FAILED_POST_EFFECT")
        self.assertEqual(observed["status"], "running")

    def test_hard_wall_reaper_keeps_key_when_cleanup_fails(self) -> None:
        start_identity = "6" * 32
        self.manager._running_hard_wall_seconds = 0.0
        started = self.manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
            str(self.cwd),
            start_identity,
            mode="pipe",
        )
        session_id = str(started["session_id"])
        with patch(
            "agent_runtime.session._terminate_process_group",
            side_effect=PermissionError("injected reaper cleanup failure"),
        ):
            affected = self.manager.reap_once()
            self.assertIn(session_id, affected)
            observed = self.manager.poll(start_identity=start_identity)

        self.assertEqual(observed["status"], "running")
        self.assertEqual(observed["lifecycle"], "START_FAILED_POST_EFFECT")


if __name__ == "__main__":
    unittest.main()
