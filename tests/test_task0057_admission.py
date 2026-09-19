from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agent_runtime import capacity, executor, fs_read, session
from agent_runtime.contracts import FsReadItem
from agent_runtime.errors import RuntimeCapacityError, RuntimeStateError


class HeavyExecutionAdmissionTests(unittest.TestCase):
    """Isolated x6 admission and cleanup proof using only test-owned children."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="agent-runtime-task0057-")
        self.root = Path(self._temp.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self._env = mock.patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._temp.cleanup)
        self.admission = capacity.HeavyExecutionAdmission(6)
        self._global_admission = mock.patch.object(
            capacity, "_HEAVY_EXECUTION_ADMISSION", self.admission
        )
        self._global_admission.start()
        self.addCleanup(self._global_admission.stop)

    def _wait_for(self, paths: list[Path], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(path.exists() for path in paths):
                return
            time.sleep(0.01)
        self.fail(f"children did not become ready: {paths!r}")

    def _wait_for_exit(self, manager: session.TerminalSessionManager, session_id: str) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if manager.poll(session_id)["status"] == "exited":
                return
            time.sleep(0.01)
        self.fail(f"session did not exit: {session_id}")

    def test_mixed_x6_admission_is_fail_fast_and_read_control_remain_available(self) -> None:
        release = self.cwd / "release"
        ready = [self.cwd / f"exec-ready-{index}" for index in range(3)]
        code = (
            "import pathlib, time, sys\n"
            "pathlib.Path(sys.argv[1]).write_text('ready')\n"
            "release = pathlib.Path(sys.argv[2])\n"
            "while not release.exists():\n"
            "    time.sleep(.01)\n"
        )
        manager = session.TerminalSessionManager(
            max_active_sessions=6, admission=self.admission, start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [
                workers.submit(
                    executor.execute_terminal,
                    [sys.executable, "-u", "-c", code, str(path), str(release)],
                    str(self.cwd),
                    10,
                )
                for path in ready
            ]
            self._wait_for(ready)
            sessions = [
                manager.start(
                    [sys.executable, "-u", "-c", "import time; time.sleep(10)"], str(self.cwd)
                )
                for _ in range(3)
            ]
            self.assertEqual(self.admission.active, 6)

            # These paths operate on existing state and do not obtain a lease.
            (self.cwd / "readable.txt").write_text("available\n", encoding="utf-8")
            healthy = capacity.CapacitySignals(
                active_processors=8, load1=1.0, cpu_busy_fraction=0.2, thermal_state="nominal",
                swap_total_bytes=1, swap_used_bytes=0, swapin_delta_pages=0, swapout_delta_pages=0,
                vm_free_bytes=2 * 1024**3, vm_inactive_bytes=0, vm_purgeable_bytes=0,
                vm_compressor_bytes=0, disk_available_bytes=10 * 1024**3, sampled_window_ms=50,
            )
            with mock.patch.object(capacity, "_collect_signals", return_value=healthy):
                self.assertEqual(capacity.observe_capacity()["capacity_parallelism_ceiling"], 2)
            self.assertEqual(
                fs_read.read_files_batch(str(self.cwd), [FsReadItem(path="readable.txt")])["items"][0]["text"],
                "available\n",
            )
            session_id = str(sessions[0]["session_id"])
            for started in sessions:
                self.assertEqual(manager.poll(str(started["session_id"]))["status"], "running")
            self.assertEqual(manager.control(session_id, "resize", rows=30, cols=100)["status"], "running")
            self.assertEqual(self.admission.active, 6)

            # A seventh request is rejected before either subprocess or PTY creation.
            with mock.patch.object(executor._PROTECTED_GUARD, "check"), mock.patch.object(
                executor.subprocess, "Popen", side_effect=AssertionError("must not spawn")
            ):
                with self.assertRaisesRegex(RuntimeCapacityError, capacity.HEAVY_CAPACITY_ERROR):
                    executor.execute_terminal([sys.executable, "-c", "pass"], str(self.cwd), 1)
            with mock.patch.object(session._PROTECTED_GUARD, "check"), mock.patch.object(
                session.pty, "openpty", side_effect=AssertionError("must not allocate a PTY")
            ):
                with self.assertRaisesRegex(RuntimeCapacityError, capacity.HEAVY_CAPACITY_ERROR):
                    manager.start([sys.executable, "-c", "pass"], str(self.cwd))

            release.write_text("release", encoding="utf-8")
            for future in futures:
                self.assertEqual(future.result(timeout=5)["exit_code"], 0)
        manager.shutdown()
        self.assertEqual(self.admission.active, 0)

    def test_task0078_six_pty_roots_are_running_before_seventh_and_cleanup_is_exact(self) -> None:
        release = self.cwd / "pty-release"
        ready = [self.cwd / f"pty-ready-{index}" for index in range(6)]
        code = (
            "import pathlib, sys, time\n"
            "ready, release = map(pathlib.Path, sys.argv[1:])\n"
            "ready.write_text('ready')\n"
            "while not release.exists(): time.sleep(.005)\n"
        )
        manager = session.TerminalSessionManager(
            max_active_sessions=6, admission=self.admission, start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        sessions = [
            manager.start(
                [sys.executable, "-u", "-c", code, str(path), str(release)],
                str(self.cwd),
            )
            for path in ready
        ]
        self._wait_for(ready)
        self.assertEqual(self.admission.active, 6)
        for started in sessions:
            self.assertEqual(manager.poll(str(started["session_id"]))["status"], "running")

        (self.cwd / "readable-task0078.txt").write_text("available\n", encoding="utf-8")
        healthy = capacity.CapacitySignals(
            active_processors=8, load1=1.0, cpu_busy_fraction=0.2, thermal_state="nominal",
            swap_total_bytes=1, swap_used_bytes=0, swapin_delta_pages=0, swapout_delta_pages=0,
            vm_free_bytes=2 * 1024**3, vm_inactive_bytes=0, vm_purgeable_bytes=0,
            vm_compressor_bytes=0, disk_available_bytes=10 * 1024**3, sampled_window_ms=50,
        )
        with mock.patch.object(capacity, "_collect_signals", return_value=healthy):
            self.assertGreaterEqual(capacity.observe_capacity()["capacity_parallelism_ceiling"], 1)
        self.assertEqual(
            fs_read.read_files_batch(
                str(self.cwd), [FsReadItem(path="readable-task0078.txt")]
            )["items"][0]["text"],
            "available\n",
        )

        with mock.patch.object(session._PROTECTED_GUARD, "check"), mock.patch.object(
            session.pty, "openpty", side_effect=AssertionError("seventh PTY must not be allocated")
        ):
            with self.assertRaisesRegex(RuntimeStateError, "configured maximum 6 active terminal sessions"):
                manager.start([sys.executable, "-c", "pass"], str(self.cwd))

        release.write_text("release", encoding="utf-8")
        for started in sessions:
            self._wait_for_exit(manager, str(started["session_id"]))
        manager.shutdown()
        self.assertEqual(self.admission.active, 0)

    def test_every_terminal_path_releases_its_lease_once(self) -> None:
        with mock.patch.object(executor._PROTECTED_GUARD, "check"), mock.patch.object(
            executor.subprocess, "Popen", side_effect=OSError("injected spawn failure")
        ):
            with self.assertRaisesRegex(OSError, "injected spawn failure"):
                executor.execute_terminal([sys.executable, "-c", "pass"], str(self.cwd), 1)
        self.assertEqual(self.admission.active, 0)

        self.assertEqual(
            executor.execute_terminal([sys.executable, "-c", "pass"], str(self.cwd), 3)["exit_code"], 0
        )
        self.assertEqual(self.admission.active, 0)
        self.assertTrue(
            executor.execute_terminal(
                [sys.executable, "-c", "import time; time.sleep(2)"], str(self.cwd), 0.05
            )["timed_out"]
        )
        self.assertEqual(self.admission.active, 0)

        manager = session.TerminalSessionManager(
            max_active_sessions=6, admission=self.admission, start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        with mock.patch.object(session._PROTECTED_GUARD, "check"), mock.patch.object(
            session.pty, "openpty", side_effect=OSError("injected pty failure")
        ):
            with self.assertRaisesRegex(OSError, "injected pty failure"):
                manager.start([sys.executable, "-c", "pass"], str(self.cwd))
        self.assertEqual(self.admission.active, 0)

        natural = manager.start([sys.executable, "-u", "-c", "print('done')"], str(self.cwd))
        self._wait_for_exit(manager, str(natural["session_id"]))
        self.assertEqual(self.admission.active, 0)

        raced = manager.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(10)"], str(self.cwd)
        )
        session_id = str(raced["session_id"])
        def terminate() -> None:
            try:
                manager.control(session_id, "terminate")
            except RuntimeError:
                pass

        workers = [threading.Thread(target=terminate), threading.Thread(target=manager.shutdown)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.admission.active, 0)

        now = [100.0]
        idle = session.TerminalSessionManager(
            clock=lambda: now[0], max_active_sessions=6, admission=self.admission, start_reaper=False
        )
        self.addCleanup(idle.shutdown)
        expiring = idle.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(10)"], str(self.cwd)
        )
        now[0] += 601.0
        self.assertEqual(idle.reap_idle_once(), [str(expiring["session_id"])])
        self.assertEqual(self.admission.active, 0)

    def test_terminal_exec_materializes_output_only_after_delayed_reader_drain(self) -> None:
        reader_barrier = threading.Barrier(3)
        release_readers = threading.Event()
        original_consume = executor._BoundedCapture.consume

        def delayed_consume(capture: executor._BoundedCapture, stream) -> None:
            reader_barrier.wait(timeout=5)
            self.assertTrue(release_readers.wait(timeout=5))
            original_consume(capture, stream)

        with mock.patch.object(executor._BoundedCapture, "consume", delayed_consume), ThreadPoolExecutor(
            max_workers=1
        ) as workers:
            future = workers.submit(
                executor.execute_terminal,
                [sys.executable, "-c", "import sys; print('stdout'); print('stderr', file=sys.stderr)"],
                str(self.cwd),
                5,
            )
            reader_barrier.wait(timeout=5)
            self.assertFalse(future.done())
            self.assertEqual(self.admission.active, 1)
            release_readers.set()
            result = future.result(timeout=5)

        self.assertEqual(result["stdout"], "stdout\n")
        self.assertEqual(result["stderr"], "stderr\n")
        self.assertEqual(self.admission.active, 0)

    def test_isolated_runtime_sigterm_cleans_an_inflight_one_shot_group(self) -> None:
        ready = self.cwd / "runtime-ready"
        child_pid = self.cwd / "one-shot-pid"
        marker = self.cwd / "one-shot-descendant-survived"
        descendant = f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('alive')"
        child = (
            "import os,pathlib,subprocess,sys,time; "
            f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
            "time.sleep(30)"
        )
        runtime_root = Path(__file__).resolve().parents[1]
        runtime_code = f"""
import pathlib, sys, threading, time, types

class FakeMCPServer:
    def __init__(self, name=None, **_metadata): self.middleware = []
    def tool(self, annotations=None): return lambda function: function
    def run(self):
        deadline = time.monotonic() + 5
        while not pathlib.Path({str(child_pid)!r}).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        pathlib.Path({str(ready)!r}).write_text('ready')
        while True: time.sleep(1)

mcp_package = types.ModuleType('mcp'); mcp_package.__path__ = []
mcp_server = types.ModuleType('mcp.server'); mcp_server.__path__ = []; mcp_server.MCPServer = FakeMCPServer
mcp_mcpserver = types.ModuleType('mcp.server.mcpserver'); mcp_mcpserver.__path__ = []
mcp_types = types.ModuleType('mcp.types')
class FakeCallToolResult: pass
class FakeImageContent: pass
mcp_types.CallToolResult = FakeCallToolResult
mcp_types.ImageContent = FakeImageContent
mcp_exceptions = types.ModuleType('mcp.server.mcpserver.exceptions')
class FakeToolError(Exception): pass
mcp_exceptions.ToolError = FakeToolError
sys.modules.update({{'mcp': mcp_package, 'mcp.server': mcp_server, 'mcp.server.mcpserver': mcp_mcpserver, 'mcp.server.mcpserver.exceptions': mcp_exceptions, 'mcp.types': mcp_types}})

from agent_runtime import server
from agent_runtime.executor import execute_terminal
threading.Thread(target=execute_terminal, args=([sys.executable, '-u', '-c', {child!r}], {str(self.cwd)!r}, 30), daemon=True).start()
server._main()
"""
        env = {
            **os.environ,
            "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(runtime_root), os.environ.get("PYTHONPATH", "")))),
        }
        runtime = subprocess.Popen(
            [sys.executable, "-u", "-c", runtime_code], cwd=str(runtime_root), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            self._wait_for([ready])
            runtime.send_signal(__import__("signal").SIGTERM)
            self.assertEqual(runtime.wait(timeout=5), -__import__("signal").SIGTERM)
            time.sleep(1.2)
            self.assertFalse(marker.exists())
        finally:
            if runtime.poll() is None:
                runtime.kill()
                runtime.wait(timeout=2)
            if runtime.stderr is not None:
                runtime.stderr.close()

    def test_repeated_x6_pty_cycles_leave_no_owned_leases_fds_or_workers(self) -> None:
        manager = session.TerminalSessionManager(
            max_active_sessions=6, admission=self.admission, start_reaper=False
        )
        self.addCleanup(manager.shutdown)
        fd_before = len(os.listdir("/dev/fd"))
        for _ in range(3):
            started = [
                manager.start([sys.executable, "-u", "-c", "print('done')"], str(self.cwd))
                for _ in range(6)
            ]
            for result in started:
                self._wait_for_exit(manager, str(result["session_id"]))
            self.assertEqual(self.admission.active, 0)

        manager.shutdown()
        deadline = time.monotonic() + 2.0
        while any(thread.name.startswith("terminal-") for thread in threading.enumerate()):
            if time.monotonic() >= deadline:
                self.fail("Runtime-owned terminal worker thread leaked after repeated cycles")
            time.sleep(0.01)
        self.assertEqual(self.admission.active, 0)
        self.assertLessEqual(len(manager._sessions), session.MAX_RETAINED_COMPLETED_SESSIONS)
        self.assertLessEqual(len(os.listdir("/dev/fd")), fd_before + 1)


if __name__ == "__main__":
    unittest.main()
