from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SupervisedLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_ctx = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_ctx.name).resolve()
        self.home = self.temp / "home"
        self.home.mkdir()
        self.state = self.home / "fake-launchd"
        self.state.mkdir()
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        (self.repo / "start.sh").write_text((ROOT / "start.sh").read_text())
        (self.repo / "start.sh").chmod(0o700)
        runtime_python = self.repo / ".venv" / "bin" / "python"
        runtime_python.parent.mkdir(parents=True)
        runtime_python.write_text("#!/bin/bash\nexit 0\n")
        runtime_python.chmod(0o700)
        (self.repo / ".env").write_text(
            "CONTROL_PLANE_API_KEY=dummy\n"
            "CONTROL_PLANE_TUNNEL_ID=stable-fixture-id\n"
            "AGENT_RUNTIME_WORKSPACE_ROOT=" + str(self.temp) + "\n"
        )
        launch_agents = self.home / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        (launch_agents / "com.picmao.agent-runtime-runtime.plist").write_text("fixture\n")
        self._write_fakes()
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_STATE": str(self.state),
            "FAKE_REPO": str(self.repo),
            "FAKE_TUNNEL_CLIENT": str(self.bin / "tunnel-client"),
        }

    def tearDown(self) -> None:
        try:
            self.run_start("stop")
        except Exception:
            pass
        self.temp_ctx.cleanup()

    def _write(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o700)

    def _write_fakes(self) -> None:
        self._write(
            "tunnel-client",
            """#!/bin/bash
set -e
state="$HOME/fake-launchd"
case "$1" in
  run)
    if ! mkdir "$state/runtime.lock" 2>/dev/null; then echo duplicate >> "$state/duplicates.log"; exit 9; fi
    echo $$ > "$state/runtime.pid"
    echo start >> "$state/starts.log"
    cleanup() { if [[ -f "$state/runtime.pid" && "$(cat "$state/runtime.pid")" == "$$" ]]; then rm -f "$state/runtime.pid"; fi; rmdir "$state/runtime.lock" 2>/dev/null || true; }
    trap cleanup EXIT
    trap 'cleanup; exit 0' TERM INT
    while true; do sleep 1; done
    ;;
  doctor) exit 0 ;;
  *) exit 2 ;;
esac
""",
        )
        self._write(
            "lsof",
            """#!/bin/bash
if [[ -n "${FAKE_FOREIGN_PORT_PID:-}" ]]; then echo "$FAKE_FOREIGN_PORT_PID"; fi
if [[ -z "${FAKE_FOREIGN_PORT_PID:-}" && -f "$HOME/fake-launchd/runtime.pid" ]]; then
  cat "$HOME/fake-launchd/runtime.pid"
fi
""",
        )
        self._write(
            "pgrep",
            """#!/bin/bash
if [[ -n "${FAKE_CANONICAL_PID:-}" ]]; then echo "$FAKE_CANONICAL_PID"; fi
""",
        )
        self._write(
            "ps",
            """#!/bin/bash
if [[ "$*" == *"777"* ]]; then
  if [[ "$*" == *"comm="* ]]; then echo "/usr/bin/python3"; else echo "/usr/bin/python3 -m http.server 8080"; fi
  exit 0
fi
runtime_pid="$(cat "$HOME/fake-launchd/runtime.pid" 2>/dev/null || true)"
if [[ -n "$runtime_pid" && "$*" == *" $runtime_pid "* ]]; then
  if [[ "$*" == *"comm="* ]]; then
    echo "$FAKE_TUNNEL_CLIENT"
  else
    echo "$FAKE_TUNNEL_CLIENT run --control-plane.poll-channel main --mcp.command command=$FAKE_REPO/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080"
  fi
  exit 0
fi
/bin/ps "$@"
""",
        )
        self._write(
            "curl",
            """#!/bin/bash
if [[ -f "$HOME/fake-launchd/runtime.pid" && ( "$*" == *"/healthz"* || "$*" == *"/readyz"* ) ]]; then
  exit 0
fi
exit 22
""",
        )
        self._write(
            "launchctl",
            """#!/bin/bash
set -e
state="$FAKE_STATE"
repo="$FAKE_REPO"
desired="$HOME/Library/Application Support/Agent Runtime/protected-runtime-running"
spawn() {
  if [[ -f "$state/runtime.pid" ]] && kill -0 "$(cat "$state/runtime.pid")" 2>/dev/null; then return 0; fi
  HOME="$HOME" PATH="$PATH" FAKE_STATE="$state" FAKE_REPO="$repo" /usr/bin/python3 - "$repo" <<'PYDETACH'
import os, subprocess, sys
repo = sys.argv[1]
subprocess.Popen(
    [repo + "/start.sh", "--serve", os.environ["FAKE_TUNNEL_CLIENT"]],
    env=os.environ.copy(),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
PYDETACH
  for _ in {1..100}; do [[ -f "$state/runtime.pid" ]] && return 0; sleep 0.01; done
  return 3
}
case "$1" in
  print) [[ -f "$state/loaded" ]] ;;
  bootstrap) ( set -o noclobber; > "$state/loaded" ) 2>/dev/null || exit 5 ;;
  kickstart) [[ -f "$state/loaded" ]] || exit 6; spawn ;;
  kill)
    if [[ -n "${FAKE_KILL_NOT_RUNNING:-}" && ! -f "$state/runtime.pid" ]]; then exit 3; fi
    if [[ -f "$state/runtime.pid" ]]; then
      pid=$(cat "$state/runtime.pid")
      /bin/kill -TERM "$pid" 2>/dev/null || true
      for _ in {1..100}; do /bin/kill -0 "$pid" 2>/dev/null || break; sleep 0.01; done
    fi
    [[ -f "$desired" ]] && spawn || true
    ;;
  *) exit 7 ;;
esac
""",
        )

    def run_start(self, action: str, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(self.repo / "start.sh"), action],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def current_pid(self) -> int | None:
        path = self.state / "runtime.pid"
        if not path.exists():
            return None
        return int(path.read_text().strip())

    def wait_for_pid_change(self, old: int | None) -> int:
        deadline = time.time() + 3
        while time.time() < deadline:
            current = self.current_pid()
            if current is not None and current != old:
                return current
            time.sleep(0.02)
        self.fail("runtime pid did not change")

    def test_ten_concurrent_starts_collapse_and_recovery_stop_start_restart_are_singleton(self) -> None:
        processes = [
            subprocess.Popen(
                [str(self.repo / "start.sh"), "start"],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(10)
        ]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _out, _err, code in results), results)
        first = self.current_pid()
        self.assertIsNotNone(first)
        self.assertEqual((self.state / "starts.log").read_text().splitlines(), ["start"])
        self.assertFalse((self.state / "duplicates.log").exists())

        service = "gui/501/com.picmao.agent-runtime-runtime"
        subprocess.run([str(self.bin / "launchctl"), "kill", "SIGTERM", service], env=self.env, check=True)
        recovered = self.wait_for_pid_change(first)
        self.assertEqual(len((self.state / "starts.log").read_text().splitlines()), 2)

        stopped = self.run_start("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        time.sleep(0.15)
        self.assertIsNone(self.current_pid())
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())

        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        after_start = self.wait_for_pid_change(recovered)
        restarted = self.run_start("restart")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        after_restart = self.wait_for_pid_change(after_start)
        self.assertNotEqual(after_start, after_restart)
        self.assertFalse((self.state / "duplicates.log").exists())
        self.assertEqual(len((self.state / "starts.log").read_text().splitlines()), 4)

    def test_foreign_8080_listener_fails_closed_without_desired_state_or_signal(self) -> None:
        result = self.run_start("start", extra_env={"FAKE_FOREIGN_PORT_PID": "777"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("8080", result.stderr)
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())
        self.assertFalse((self.state / "starts.log").exists())

    def test_stop_when_loaded_but_already_not_running_is_idempotent(self) -> None:
        (self.state / "loaded").write_text("")
        result = self.run_start("stop", extra_env={"FAKE_KILL_NOT_RUNNING": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STOPPED", result.stdout)
        self.assertIsNone(self.current_pid())


if __name__ == "__main__":
    unittest.main()
