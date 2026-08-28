from __future__ import annotations

import errno
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_runtime.executor import (
    MAX_OUTPUT_BYTES,
    _process_group_exists,
    _terminate_process_group,
    execute_terminal,
)


class TerminalExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "project"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_exec(self, argv: list[str], *, timeout: float = 5.0):
        with patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
            clear=False,
        ):
            return execute_terminal(argv, str(self.cwd), timeout)

    def test_requires_non_empty_argv_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "argv"):
            self.run_exec([])
        with self.assertRaisesRegex(ValueError, "argv"):
            self.run_exec([sys.executable, 7])  # type: ignore[list-item]
        with self.assertRaisesRegex(ValueError, "argv"):
            self.run_exec([""])

    def test_requires_absolute_existing_workspace_root(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "AGENT_RUNTIME_WORKSPACE_ROOT"):
                execute_terminal([sys.executable, "-c", "pass"], str(self.cwd), 1)

        with patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": "relative/root"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "absolute"):
                execute_terminal([sys.executable, "-c", "pass"], str(self.cwd), 1)

    def test_requires_absolute_cwd_inside_workspace_after_realpath(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "cwd"):
                execute_terminal([sys.executable, "-c", "pass"], "relative", 1)

            outside = Path(tempfile.mkdtemp()).resolve()
            self.addCleanup(shutil_rmtree, outside)
            with self.assertRaisesRegex(ValueError, "outside"):
                execute_terminal([sys.executable, "-c", "pass"], str(outside), 1)

            link = self.root / "escape"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside"):
                execute_terminal([sys.executable, "-c", "pass"], str(link), 1)

    def test_executes_literal_argv_without_shell_interpretation(self) -> None:
        marker = self.cwd / "should-not-exist"
        literal = f"hello; touch {marker}"
        result = self.run_exec(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                literal,
            ]
        )

        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["stdout"].strip(), literal)
        self.assertFalse(marker.exists())
        self.assertEqual(result["argv"][-1], literal)
        self.assertEqual(result["cwd"], str(self.cwd))

    def test_child_environment_is_minimal_and_strips_runtime_and_secret_values(self) -> None:
        secret_names = {
            "CONTROL_PLANE_API_KEY": "secret-a",
            "AGENT_RUNTIME_INTERNAL": "secret-b",
            "OPENAI_API_KEY": "secret-c",
            "AWS_ACCESS_KEY_ID": "secret-d",
            "MY_TOKEN": "secret-e",
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
        code = (
            "import json, os; "
            "names=" + repr(sorted(set(secret_names) | set(preserved))) + "; "
            "print(json.dumps({n: os.environ.get(n) for n in names}))"
        )
        with patch.dict(os.environ, env, clear=True):
            result = execute_terminal([sys.executable, "-c", code], str(self.cwd), 5)

        observed = json.loads(result["stdout"])
        for name in secret_names:
            self.assertIsNone(observed[name], name)
        for name, value in preserved.items():
            self.assertEqual(observed[name], value, name)

    def test_stdin_is_disconnected(self) -> None:
        result = self.run_exec(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"]
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "0")

    def test_stdout_and_stderr_are_bounded_with_truncation_flags(self) -> None:
        size = MAX_OUTPUT_BYTES + 4096
        result = self.run_exec(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('o'*{size}); sys.stderr.write('e'*{size})",
            ]
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), MAX_OUTPUT_BYTES)
        self.assertLessEqual(len(result["stderr"].encode("utf-8")), MAX_OUTPUT_BYTES)

    def test_process_group_probe_distinguishes_esrch_from_eperm(self) -> None:
        with patch(
            "agent_runtime.executor.os.killpg",
            side_effect=ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH)),
        ):
            self.assertFalse(_process_group_exists(12345))

        with patch(
            "agent_runtime.executor.os.killpg",
            side_effect=PermissionError(errno.EPERM, os.strerror(errno.EPERM)),
        ):
            self.assertTrue(_process_group_exists(12345))

    def test_process_group_termination_reaps_leader_before_escalation(self) -> None:
        process = Mock(pid=12345)
        leader_reaped = False

        def poll() -> int:
            nonlocal leader_reaped
            leader_reaped = True
            return -signal.SIGTERM

        process.poll.side_effect = poll

        def killpg(_pgid: int, sent_signal: int) -> None:
            if sent_signal == signal.SIGTERM:
                return None
            if sent_signal == 0:
                if leader_reaped:
                    raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))
                raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
            if sent_signal == signal.SIGKILL:
                raise AssertionError("SIGKILL must not run after reaping makes the group absent")
            raise AssertionError(sent_signal)

        with (
            patch("agent_runtime.executor.os.killpg", side_effect=killpg),
            patch("agent_runtime.executor.time.sleep", return_value=None),
        ):
            _terminate_process_group(process)

        self.assertGreaterEqual(process.poll.call_count, 1)

    def test_process_group_termination_permission_errors_are_not_swallowed(self) -> None:
        process = Mock(pid=12345)
        process.poll.return_value = None

        for denied_signal in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(denied_signal=denied_signal):
                def killpg(_pgid: int, sent_signal: int) -> None:
                    if sent_signal == 0:
                        return None
                    if sent_signal == denied_signal:
                        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

                with (
                    patch("agent_runtime.executor.os.killpg", side_effect=killpg),
                    patch("agent_runtime.executor._TERMINATE_GRACE_SECONDS", 0.0),
                ):
                    with self.assertRaises(PermissionError):
                        _terminate_process_group(process)

    def test_timeout_terminates_process_group_and_returns_truthful_timeout(self) -> None:
        marker = self.cwd / "grandchild-survived"
        child_code = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', "
            + repr(f"import time, pathlib; time.sleep(1.2); pathlib.Path({str(marker)!r}).write_text('alive')")
            + "]); time.sleep(10)"
        )
        result = self.run_exec([sys.executable, "-c", child_code], timeout=0.2)
        self.assertTrue(result["timed_out"])
        self.assertIsInstance(result["exit_code"], int)
        time.sleep(1.5)
        self.assertFalse(marker.exists())

    def test_successful_leader_exit_does_not_leave_process_group_child_running(self) -> None:
        marker = self.cwd / "background-child-survived"
        child_code = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', "
            + repr(f"import time, pathlib; time.sleep(1.2); pathlib.Path({str(marker)!r}).write_text('alive')")
            + "])"
        )
        result = self.run_exec([sys.executable, "-c", child_code], timeout=5)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        time.sleep(1.5)
        self.assertFalse(marker.exists())

    def test_timeout_must_be_positive_and_bounded(self) -> None:
        for invalid in (0, -1, 3601):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    self.run_exec([sys.executable, "-c", "pass"], timeout=invalid)


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
