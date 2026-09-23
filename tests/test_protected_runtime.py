from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_runtime.protection as protection
from agent_runtime.protection import (
    CURRENT_RUNTIME_LAUNCHD_LABEL,
    LEGACY_RUNTIME_LAUNCHD_LABEL,
    MODERN_RUNTIME_LAUNCHD_LABEL,
    PROTECTED_RUNTIME_LAUNCHD_LABELS,
    ProtectedRuntimeDenied,
    ProtectedRuntimeGuard,
)


class ProtectedRuntimeGuardTests(unittest.TestCase):
    def make_guard(self, root: Path) -> ProtectedRuntimeGuard:
        runtime_root = Path("/Users/test/agent-runtime")
        command = (
            "/opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main "
            f"--mcp.command command={runtime_root}/.venv/bin/python -m agent_runtime.server,channel=main "
            "--health.listen-addr 127.0.0.1:8080"
        )
        return ProtectedRuntimeGuard(
            runtime_root=runtime_root,
            launchd_label=MODERN_RUNTIME_LAUNCHD_LABEL,
            audit_file=root / "protected-attempts.json",
            process_rows_provider=lambda: [
                (410, 1, command),
                (411, 410, "/opt/homebrew/bin/python3 -m agent_runtime.server"),
                (999, 1, "/usr/bin/sleep 30"),
            ],
        )

    def assert_denied(self, guard: ProtectedRuntimeGuard, argv: list[str], category: str) -> None:
        with self.assertRaisesRegex(ProtectedRuntimeDenied, r"^PROTECTED_RUNTIME:") as caught:
            guard.check(argv, tool_name="terminal_exec")
        self.assertEqual(caught.exception.category, category)

    def test_process_snapshot_is_lazy_for_argv_text_only_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calls = 0

            def unexpected_rows() -> list[tuple[int, int, str]]:
                nonlocal calls
                calls += 1
                raise AssertionError("process snapshot should be lazy")

            guard = ProtectedRuntimeGuard(
                runtime_root=Path("/Users/test/agent-runtime"),
                audit_file=Path(raw) / "protected-attempts.json",
                process_rows_provider=unexpected_rows,
            )
            guard.check(["echo", "ordinary"], tool_name="terminal_exec")
            self.assert_denied(
                guard,
                ["python3", "-m", "agent_runtime.server"],
                "canonical_runtime_launch",
            )
            self.assert_denied(
                guard,
                ["launchctl", "stop", MODERN_RUNTIME_LAUNCHD_LABEL],
                "canonical_service_lifecycle",
            )
            self.assert_denied(guard, ["./start.sh", "stop"], "canonical_service_lifecycle")
            self.assert_denied(
                guard,
                ["python3", "-m", "http.server", "8080"],
                "protected_port_rebind",
            )
            self.assertEqual(calls, 0)

    def test_process_sensitive_classification_requests_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calls = 0
            runtime_root = Path("/Users/test/agent-runtime")
            command = (
                "/opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main "
                f"--mcp.command command={runtime_root}/.venv/bin/python -m agent_runtime.server,channel=main "
                "--health.listen-addr 127.0.0.1:8080"
            )

            def rows() -> list[tuple[int, int, str]]:
                nonlocal calls
                calls += 1
                return [(410, 1, command)]

            guard = ProtectedRuntimeGuard(
                runtime_root=runtime_root,
                audit_file=Path(raw) / "protected-attempts.json",
                process_rows_provider=rows,
            )
            self.assert_denied(guard, ["kill", "-TERM", "410"], "canonical_process_signal")
            self.assertEqual(calls, 1)

    def test_default_process_reader_avoids_unbounded_subprocess_run_capture(self) -> None:
        real_popen = subprocess.Popen
        children: list[subprocess.Popen[bytes]] = []

        def popen_factory(*_args, **_kwargs):
            child = real_popen(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'1 0 init' + bytes([10]))"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            children.append(child)
            return child

        with mock.patch.object(
            protection.subprocess,
            "run",
            side_effect=AssertionError("whole-output capture is unbounded"),
        ), mock.patch.object(protection.subprocess, "Popen", side_effect=popen_factory):
            self.assertEqual(protection._read_process_rows(), [(1, 0, "init")])
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_default_process_reader_enforces_byte_bound_before_materialization(self) -> None:
        max_bytes = protection.PROCESS_SNAPSHOT_MAX_BYTES
        real_popen = subprocess.Popen

        def run_payload(payload_size: int):
            children: list[subprocess.Popen[bytes]] = []

            def popen_factory(*_args, **_kwargs):
                command_size = payload_size - len(b"1 0 ") - len(b"\n")
                script = (
                    "import sys; "
                    f"sys.stdout.buffer.write(b'1 0 ' + b'x' * {command_size} + b'\\n')"
                )
                child = real_popen(
                    [sys.executable, "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                children.append(child)
                return child

            with mock.patch.object(protection.subprocess, "Popen", side_effect=popen_factory):
                try:
                    result = protection._read_process_rows()
                finally:
                    self.assertTrue(children)
                    self.assertTrue(all(child.poll() is not None for child in children))
            return result

        below = max_bytes - 1
        at = max_bytes
        self.assertEqual(len(run_payload(below)), 1)
        self.assertEqual(len(run_payload(at)), 1)
        with self.assertRaises(protection._ProcessSnapshotUnavailable):
            run_payload(max_bytes + 1)

    def test_default_process_reader_enforces_row_bound_and_reaps_helper(self) -> None:
        real_popen = subprocess.Popen
        children: list[subprocess.Popen[bytes]] = []
        row_count = protection.PROCESS_SNAPSHOT_MAX_ROWS + 1

        def popen_factory(*_args, **_kwargs):
            script = f"import sys; sys.stdout.buffer.write(b'1 0 init\\n' * {row_count})"
            child = real_popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            children.append(child)
            return child

        with mock.patch.object(protection.subprocess, "Popen", side_effect=popen_factory):
            with self.assertRaises(protection._ProcessSnapshotUnavailable):
                protection._read_process_rows()
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_default_process_reader_deadline_and_nonzero_exit_reap_helper(self) -> None:
        real_popen = subprocess.Popen

        for script in (
            "import time; time.sleep(5)",
            "import sys; sys.exit(7)",
        ):
            with self.subTest(script=script):
                children: list[subprocess.Popen[bytes]] = []

                def popen_factory(*_args, **_kwargs):
                    child = real_popen(
                        [sys.executable, "-c", script],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    children.append(child)
                    return child

                with mock.patch.object(
                    protection, "PROCESS_SNAPSHOT_DEADLINE_SECONDS", 0.05
                ), mock.patch.object(protection.subprocess, "Popen", side_effect=popen_factory):
                    with self.assertRaises(protection._ProcessSnapshotUnavailable):
                        protection._read_process_rows()
                self.assertTrue(children)
                self.assertTrue(all(child.poll() is not None for child in children))

    def test_process_sensitive_classification_fails_closed_when_snapshot_unavailable(self) -> None:
        cases = (
            ["kill", "-TERM", "999"],
            ["pkill", "-f", "agent_runtime.server"],
            ["killall", "tunnel-client"],
        )
        with tempfile.TemporaryDirectory() as raw:
            for argv in cases:
                with self.subTest(argv=argv):
                    def unavailable() -> list[tuple[int, int, str]]:
                        raise RuntimeError("synthetic process snapshot failure")

                    guard = ProtectedRuntimeGuard(
                        runtime_root=Path("/Users/test/agent-runtime"),
                        audit_file=Path(raw) / "protected-attempts.json",
                        process_rows_provider=unavailable,
                    )
                    self.assert_denied(guard, argv, "process_inspection_unavailable")

    def test_direct_pid_and_process_group_signals_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            self.assert_denied(guard, ["kill", "-TERM", "410"], "canonical_process_signal")
            self.assert_denied(guard, ["kill", "-KILL", "-410"], "canonical_process_signal")
            self.assert_denied(guard, ["kill", "-9", "411"], "canonical_process_signal")
            guard.check(["kill", "-TERM", "999"], tool_name="terminal_exec")

    def test_matching_pkill_killall_and_shell_wrappers_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            self.assert_denied(guard, ["pkill", "-f", "agent_runtime.server"], "canonical_process_match")
            self.assert_denied(guard, ["killall", "tunnel-client"], "canonical_process_match")
            self.assert_denied(guard, ["/bin/zsh", "-lc", "kill -TERM 410"], "canonical_process_signal")
            guard.check(["pkill", "-f", "definitely-unrelated-pattern"], tool_name="terminal_exec")

    def test_compound_shell_and_canonical_relaunch_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            self.assert_denied(
                guard,
                ["/bin/zsh", "-lc", "echo safe; kill -TERM 410"],
                "canonical_process_signal",
            )
            self.assert_denied(
                guard,
                ["/usr/bin/env", "tunnel-client", "run", "--control-plane.poll-channel", "main", "--mcp.command", "command=/Users/test/agent-runtime/.venv/bin/python -m agent_runtime.server,channel=main", "--health.listen-addr", "127.0.0.1:8080"],
                "canonical_runtime_launch",
            )
            self.assert_denied(
                guard,
                ["python3", "-m", "agent_runtime.server"],
                "canonical_runtime_launch",
            )
            self.assert_denied(
                guard,
                ["env", "-u", "IGNORED", "tunnel-client", "run", "--control-plane.poll-channel", "main", "--mcp.command", "command=/Users/test/agent-runtime/.venv/bin/python -m agent_runtime.server,channel=main", "--health.listen-addr", "127.0.0.1:8080"],
                "canonical_runtime_launch",
            )
            self.assert_denied(
                guard,
                ["nice", "-n", "5", "tunnel-client", "run", "--control-plane.poll-channel", "main", "--mcp.command", "command=/Users/test/agent-runtime/.venv/bin/python -m agent_runtime.server,channel=main", "--health.listen-addr", "127.0.0.1:8080"],
                "canonical_runtime_launch",
            )
            guard.check(["env", "FOO=bar", "echo", "ordinary"], tool_name="terminal_exec")

    def test_launchd_and_cli_lifecycle_mutations_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            self.assert_denied(
                guard,
                ["launchctl", "bootout", "gui/501/com.picmao.agent-runtime-runtime"],
                "canonical_service_lifecycle",
            )
            self.assert_denied(
                guard,
                ["launchctl", "kill", "SIGTERM", "gui/501/com.picmao.agent-runtime-runtime"],
                "canonical_service_lifecycle",
            )
            self.assert_denied(guard, ["./start.sh", "stop"], "canonical_service_lifecycle")
            self.assert_denied(guard, ["./start.sh", "restart"], "canonical_service_lifecycle")
            self.assert_denied(guard, ["./start.sh", "start"], "canonical_service_lifecycle")
            guard.check(["launchctl", "print", "gui/501/com.apple.WindowServer"], tool_name="terminal_exec")

    def test_runtime_launchd_protection_covers_modern_and_legacy_only(self) -> None:
        self.assertEqual(CURRENT_RUNTIME_LAUNCHD_LABEL, MODERN_RUNTIME_LAUNCHD_LABEL)
        self.assertEqual(
            PROTECTED_RUNTIME_LAUNCHD_LABELS,
            frozenset({LEGACY_RUNTIME_LAUNCHD_LABEL, MODERN_RUNTIME_LAUNCHD_LABEL}),
        )
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            for label in (MODERN_RUNTIME_LAUNCHD_LABEL, LEGACY_RUNTIME_LAUNCHD_LABEL):
                with self.subTest(label=label):
                    self.assert_denied(
                        guard,
                        ["launchctl", "kickstart", f"gui/501/{label}"],
                        "canonical_service_lifecycle",
                    )
            guard.check(
                ["launchctl", "kickstart", "gui/501/com.picmao.unrelated-runtime"],
                tool_name="terminal_exec",
            )

    def test_read_only_lsof_on_protected_port_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            guard.check(["lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN"], tool_name="terminal_exec")
            guard.check(
                ["/bin/zsh", "-lc", "lsof -nP -iTCP:8080 -sTCP:LISTEN"],
                tool_name="terminal_exec",
            )

    def test_explicit_port_8080_free_or_rebind_attempts_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            self.assert_denied(
                guard,
                ["/bin/zsh", "-lc", "lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill -9"],
                "protected_port_lifecycle",
            )
            self.assert_denied(
                guard,
                ["python3", "-m", "http.server", "8080"],
                "protected_port_rebind",
            )
            guard.check(["python3", "-m", "http.server", "8099"], tool_name="terminal_exec")

    def test_classifier_only_documents_same_uid_interpreter_indirection_limitations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = self.make_guard(Path(raw))
            cases = (
                ["python3", "-c", "import os; os.kill(410, 15)"],
                ["perl", "-e", "kill 15, 410"],
                ["/bin/zsh", "/tmp/synthetic-runtime-maintenance.sh"],
            )
            for argv in cases:
                with self.subTest(argv=argv):
                    # Classification only: these synthetic argv values are never spawned.
                    self.assertIsNone(guard._classify(argv))

    def test_audit_is_bounded_and_contains_no_command_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            guard = self.make_guard(root)
            for index in range(30):
                with self.assertRaises(ProtectedRuntimeDenied):
                    guard.check(["/bin/zsh", "-lc", f"kill -TERM 410 # secret-{index}"], tool_name="terminal_exec")
            payload = json.loads((root / "protected-attempts.json").read_text())
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["blocked_count"], 20)
            self.assertLessEqual(len(payload["events"]), 20)
            serialized = json.dumps(payload)
            self.assertNotIn("secret-", serialized)
            self.assertNotIn("kill -TERM", serialized)
            self.assertEqual(payload["events"][-1]["category"], "canonical_process_signal")
            self.assertEqual(payload["events"][-1]["tool"], "terminal_exec")

    def test_terminal_control_write_is_guarded_before_pty_input(self) -> None:
        import agent_runtime.session as session

        manager = session.TerminalSessionManager(start_reaper=False)
        fake_session = mock.Mock()
        fake_session.master_fd = 123
        fake_session.cleanup_lock = __import__("threading").RLock()
        with mock.patch.object(manager, "_get_session", return_value=fake_session), mock.patch.object(
            session, "_PROTECTED_GUARD"
        ) as guard, mock.patch.object(session.os, "write") as write:
            guard.check.side_effect = ProtectedRuntimeDenied("canonical_process_signal")
            with self.assertRaises(ProtectedRuntimeDenied):
                manager.control("session", "write", data="kill -TERM 410\n")
            write.assert_not_called()

    def test_terminal_control_fragmented_shell_write_blocks_before_completion(self) -> None:
        import agent_runtime.session as session
        import threading

        with tempfile.TemporaryDirectory() as raw:
            manager = session.TerminalSessionManager(start_reaper=False)
            process = mock.Mock()
            process.poll.return_value = None
            fixture = session._Session(
                session_id="fragmented",
                process=process,
                master_fd=123,
                cwd=os.getcwd(),
                argv=["/bin/zsh"],
                last_activity=0.0,
            )
            manager._sessions[fixture.session_id] = fixture
            guard = self.make_guard(Path(raw))
            try:
                with mock.patch.object(session, "_PROTECTED_GUARD", guard), mock.patch.object(
                    session.os, "write", side_effect=lambda _fd, view: len(view)
                ) as write:
                    manager.control("fragmented", "write", data="kill -TERM ")
                    self.assertEqual(fixture.protected_input_buffer, "kill -TERM ")
                    with self.assertRaises(ProtectedRuntimeDenied):
                        manager.control("fragmented", "write", data="410\n")
                    self.assertEqual(write.call_count, 1)
            finally:
                manager._sessions.clear()

    def test_concurrent_fragmented_writes_share_one_guard_serialization_order(self) -> None:
        import agent_runtime.session as session
        import threading

        manager = session.TerminalSessionManager(start_reaper=False)
        process = mock.Mock()
        process.poll.return_value = None
        fixture = session._Session(
            session_id="concurrent-fragments",
            process=process,
            master_fd=123,
            cwd=os.getcwd(),
            argv=["/bin/zsh"],
            last_activity=0.0,
        )
        manager._sessions[fixture.session_id] = fixture

        first_guard_entered = threading.Event()
        second_guard_entered = threading.Event()
        guard_lock = threading.Lock()
        result_lock = threading.Lock()
        guard_calls = 0
        writes: list[bytes] = []
        errors: list[BaseException] = []

        class SyntheticGuard:
            def check(self, argv: list[str], *, tool_name: str) -> None:
                nonlocal guard_calls
                if tool_name != "terminal_control":
                    return
                pending = argv[-1]
                with guard_lock:
                    guard_calls += 1
                    call_number = guard_calls
                if call_number == 1:
                    first_guard_entered.set()
                    second_guard_entered.wait(timeout=1.0)
                else:
                    second_guard_entered.set()
                if "synthetic-protected-token" in pending:
                    raise ProtectedRuntimeDenied("synthetic_protected")

        def fake_write(_fd: int, view) -> int:
            payload = bytes(view)
            with result_lock:
                writes.append(payload)
            return len(view)

        def worker(data: str) -> None:
            try:
                manager.control(fixture.session_id, "write", data=data)
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)

        with mock.patch.object(session, "_PROTECTED_GUARD", SyntheticGuard()), mock.patch.object(
            session.os, "write", side_effect=fake_write
        ):
            first = threading.Thread(target=worker, args=("synthetic-",))
            second = threading.Thread(target=worker, args=("protected-token\n",))
            first.start()
            self.assertTrue(first_guard_entered.wait(timeout=1.0))
            second.start()
            first.join(timeout=2.0)
            second.join(timeout=2.0)

        self.assertFalse(first.is_alive(), "first write deadlocked")
        self.assertFalse(second.is_alive(), "second write deadlocked")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ProtectedRuntimeDenied)
        self.assertEqual(len(writes), 1)
        self.assertEqual(fixture.protected_input_buffer, "synthetic-")

    def test_executor_and_session_boundaries_call_guard_before_spawn(self) -> None:
        import agent_runtime.executor as executor
        import agent_runtime.session as session

        with mock.patch.object(executor, "_PROTECTED_GUARD") as exec_guard, mock.patch.object(
            executor.subprocess, "Popen"
        ) as popen:
            exec_guard.check.side_effect = ProtectedRuntimeDenied("canonical_process_signal")
            with self.assertRaises(ProtectedRuntimeDenied):
                executor.execute_terminal(["kill", "410"], os.getcwd(), 1)
            popen.assert_not_called()

        manager = session.TerminalSessionManager(start_reaper=False)
        try:
            start_identity = "5" * 32
            with mock.patch.object(session, "_PROTECTED_GUARD") as start_guard, mock.patch.object(
                session.subprocess, "Popen"
            ) as popen:
                start_guard.check.side_effect = ProtectedRuntimeDenied("canonical_process_signal")
                with self.assertRaises(ProtectedRuntimeDenied):
                    manager.start(["kill", "410"], os.getcwd(), start_identity)
                popen.assert_not_called()
            with self.assertRaisesRegex(ValueError, "START_IDENTITY_UNKNOWN"):
                manager.poll(start_identity=start_identity)
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
