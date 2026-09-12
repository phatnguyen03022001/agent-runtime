from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_runtime.protection import ProtectedRuntimeDenied, ProtectedRuntimeGuard


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
            launchd_label="com.picmao.agent-runtime-runtime",
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

    def test_audit_is_bounded_and_contains_no_command_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            guard = self.make_guard(root)
            for index in range(30):
                with self.assertRaises(ProtectedRuntimeDenied):
                    guard.check(["/bin/zsh", "-lc", f"kill -TERM 410 # secret-{index}"], tool_name="terminal_exec")
            payload = json.loads((root / "protected-attempts.json").read_text())
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["blocked_count"], 30)
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
            with mock.patch.object(session, "_PROTECTED_GUARD") as start_guard, mock.patch.object(
                session.subprocess, "Popen"
            ) as popen:
                start_guard.check.side_effect = ProtectedRuntimeDenied("canonical_process_signal")
                with self.assertRaises(ProtectedRuntimeDenied):
                    manager.start(["kill", "410"], os.getcwd())
                popen.assert_not_called()
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
