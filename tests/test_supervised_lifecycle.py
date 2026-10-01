from __future__ import annotations

import json
import os
import plistlib
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
        (self.repo / "agent_runtime").mkdir()
        (self.repo / "agent_runtime/server.py").write_text("# fixture runtime payload\n")
        runtime_python = self.repo / ".venv" / "bin" / "python"
        runtime_python.parent.mkdir(parents=True)
        runtime_python.write_text("#!/bin/bash\nexit 0\n")
        runtime_python.chmod(0o700)
        (self.repo / ".env").write_text(
            "CONTROL_PLANE_API_KEY=dummy\n"
            "CONTROL_PLANE_TUNNEL_ID=stable-fixture-id\n"
            "AGENT_RUNTIME_WORKSPACE_ROOT=" + str(self.temp) + "\n"
        )
        modern_main = self.home / "Applications" / "Agent Runtime.app" / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
        modern_main.parent.mkdir(parents=True)
        modern_main.write_text(
            """#!/bin/bash
[[ "$1" == "--service-management" && "$2" == "status" ]] || exit 2
main_state="${FAKE_MAIN_SM_STATE:-${FAKE_SM_STATE:-enabled}}"
runtime_state="${FAKE_RUNTIME_SM_STATE:-${FAKE_SM_STATE:-enabled}}"
printf '{"main_app":"%s","runtime_agent":"%s"}\n' "$main_state" "$runtime_state"
"""
        )
        modern_main.chmod(0o700)
        installed_app = self.home / "Applications" / "Agent Runtime.app"
        helper = installed_app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
        helper.write_text("#!/bin/bash\nexit 0\n")
        helper.chmod(0o700)
        service_plist = self.home / "Library" / "LaunchAgents" / "com.picmao.agent-runtime-runtime-service.plist"
        service_plist.parent.mkdir(parents=True)
        service_plist.write_bytes(plistlib.dumps({
            "Label": "com.picmao.agent-runtime-runtime-service",
            "ProgramArguments": [str(helper)],
            "RunAtLoad": False,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "ThrottleInterval": 2,
        }))
        service_plist.chmod(0o600)
        self.service_plist = service_plist
        (self.state / "loaded").write_text("")
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
args=("$@")
requested_pid=""
output_format=""
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "-p" && $((i + 1)) -lt ${#args[@]} ]]; then
    requested_pid="${args[$((i + 1))]}"
  elif [[ "${args[$i]}" == "-o" && $((i + 1)) -lt ${#args[@]} ]]; then
    output_format="${args[$((i + 1))]}"
  fi
done
if [[ "$requested_pid" == "777" ]]; then
  if [[ "$output_format" == "comm=" ]]; then echo "/usr/bin/python3"; else echo "/usr/bin/python3 -m http.server 8080"; fi
  exit 0
fi
runtime_pid="$(cat "$HOME/fake-launchd/runtime.pid" 2>/dev/null || true)"
if [[ -n "$runtime_pid" && "$requested_pid" == "$runtime_pid" ]]; then
  if [[ "$output_format" == "comm=" ]]; then
    echo "$FAKE_TUNNEL_CLIENT"
  elif [[ "$output_format" == "ppid=" ]]; then
    echo "${FAKE_PARENT_PID:-${FAKE_SERVICE_PID:-4242}}"
  elif [[ -n "${FAKE_ARGV_DRIFT:-}" ]]; then
    echo "$FAKE_TUNNEL_CLIENT <process presentation changed>"
  else
    echo "$FAKE_TUNNEL_CLIENT run --control-plane.poll-channel main --mcp.command command=$FAKE_REPO/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080"
  fi
  exit 0
fi
/bin/ps "${args[@]}"
""",
        )
        self._write(
            "curl",
            """#!/bin/bash
if [[ "$*" == *"/healthz"* && -n "${FAKE_HEALTH_FAIL:-}" ]]; then exit 22; fi
if [[ "$*" == *"/readyz"* && -n "${FAKE_READY_FAIL:-}" ]]; then exit 22; fi
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
service="gui/501/com.picmao.agent-runtime-runtime-service"
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
  print)
    [[ "${2:-}" == "$service" && -f "$state/loaded" ]] || exit 1
    if [[ -f "$state/runtime.pid" ]]; then
      echo "state = running"
      echo "pid = ${FAKE_SERVICE_PID:-4242}"
    else
      echo "state = not running"
    fi
    ;;
  bootstrap) ( set -o noclobber; > "$state/loaded" ) 2>/dev/null || exit 5 ;;
  kickstart) [[ "${2:-}" == "$service" && -f "$state/loaded" ]] || exit 6; spawn ;;
  kill)
    [[ "${3:-}" == "$service" ]] || exit 8
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

    def run_status_json(self, *, extra_env: dict[str, str] | None = None) -> dict[str, object]:
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [str(self.repo / "start.sh"), "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

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

    def test_status_reports_healthy_managed_runtime_from_serving_truth(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        pid = self.current_pid()
        self.assertIsNotNone(pid)

        status = self.run_status_json()

        self.assertEqual(status["schema"], 2)
        self.assertNotIn("desired", status)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["control"], "managed")
        self.assertEqual(status["pids"], [pid])
        self.assertEqual(status["health"], "live")
        self.assertEqual(status["ready"], "ready")

    def test_status_reports_healthy_runtime_read_only_when_control_ownership_is_unproven(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        pid = self.current_pid()
        self.assertIsNotNone(pid)

        status = self.run_status_json(extra_env={"FAKE_PARENT_PID": "9999"})

        self.assertEqual(status["state"], "running")
        self.assertEqual(status["control"], "read-only")
        self.assertEqual(status["pids"], [pid])
        self.assertEqual(status["health"], "live")
        self.assertEqual(status["ready"], "ready")

    def test_status_serving_truth_has_no_persistent_intent_field(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)

        status = self.run_status_json()

        self.assertEqual(status["schema"], 2)
        self.assertNotIn("desired", status)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["control"], "managed")

    def test_status_reports_attention_when_canonical_runtime_is_not_ready(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)

        status = self.run_status_json(extra_env={"FAKE_READY_FAIL": "1"})

        self.assertEqual(status["state"], "attention")
        self.assertEqual(status["control"], "none")
        self.assertEqual(status["health"], "live")
        self.assertEqual(status["ready"], "failed")

    def test_status_reports_stopped_when_no_runtime_exists(self) -> None:
        status = self.run_status_json()

        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["control"], "none")
        self.assertEqual(status["pids"], [])

    def test_status_remains_stopped_even_if_a_predecessor_marker_is_left_behind(self) -> None:
        desired = self.home / "Library/Application Support/Agent Runtime/protected-runtime-running"
        desired.parent.mkdir(parents=True, exist_ok=True)
        desired.touch()

        status = self.run_status_json()

        self.assertEqual(status["schema"], 2)
        self.assertNotIn("desired", status)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["control"], "none")
        self.assertEqual(status["pids"], [])

    def test_status_rejects_foreign_port_owner_even_if_port_is_occupied(self) -> None:
        status = self.run_status_json(extra_env={"FAKE_FOREIGN_PORT_PID": "777"})

        self.assertEqual(status["state"], "attention")
        self.assertEqual(status["control"], "none")
        self.assertEqual(status["pids"], [777])

    def test_status_managed_ownership_survives_process_argv_presentation_drift(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)

        status = self.run_status_json(extra_env={"FAKE_ARGV_DRIFT": "1"})

        self.assertEqual(status["state"], "running")
        self.assertEqual(status["control"], "managed")
        self.assertEqual(status["health"], "live")
        self.assertEqual(status["ready"], "ready")

    def test_stop_and_restart_refuse_read_only_runtime_without_signaling_it(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        original = self.current_pid()
        self.assertIsNotNone(original)
        desired = self.home / "Library/Application Support/Agent Runtime/protected-runtime-running"
        self.assertFalse(desired.exists())

        stopped = self.run_start("stop", extra_env={"FAKE_PARENT_PID": "9999"})
        self.assertNotEqual(stopped.returncode, 0)
        self.assertEqual(self.current_pid(), original)
        self.assertFalse(desired.exists())

        restarted = self.run_start("restart", extra_env={"FAKE_PARENT_PID": "9999"})
        self.assertNotEqual(restarted.returncode, 0)
        self.assertEqual(self.current_pid(), original)
        self.assertFalse(desired.exists())

    def test_ten_concurrent_starts_collapse_and_crash_stays_stopped_until_explicit_start(self) -> None:
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

        service = "gui/501/com.picmao.agent-runtime-runtime-service"
        subprocess.run([str(self.bin / "launchctl"), "kill", "SIGTERM", service], env=self.env, check=True)
        time.sleep(0.15)
        self.assertIsNone(self.current_pid())
        self.assertEqual((self.state / "starts.log").read_text().splitlines(), ["start"])
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())

        stopped = self.run_start("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIsNone(self.current_pid())

        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        after_start = self.current_pid()
        self.assertIsNotNone(after_start)
        restarted = self.run_start("restart")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        after_restart = self.wait_for_pid_change(after_start)
        self.assertNotEqual(after_start, after_restart)
        self.assertFalse((self.state / "duplicates.log").exists())
        self.assertEqual(len((self.state / "starts.log").read_text().splitlines()), 3)


    def test_start_uses_traditional_launchagent_without_service_management_state(self) -> None:
        result = self.run_start("start", extra_env={"FAKE_SM_STATE": "requires-approval"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(self.current_pid())

    def test_restart_uses_traditional_launchagent_without_service_management_state(self) -> None:
        started = self.run_start("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        first = self.current_pid()
        self.assertIsNotNone(first)
        restarted = self.run_start("restart", extra_env={"FAKE_SM_STATE": "not-registered"})
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertNotEqual(self.wait_for_pid_change(first), first)

    def test_start_fails_closed_when_current_launchagent_program_is_foreign(self) -> None:
        payload = plistlib.loads(self.service_plist.read_bytes())
        payload["ProgramArguments"] = ["/usr/bin/false"]
        self.service_plist.write_bytes(plistlib.dumps(payload))
        self.service_plist.chmod(0o600)
        result = self.run_start("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign", result.stderr)
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())
        self.assertFalse((self.state / "starts.log").exists())

    def test_start_fails_closed_when_current_launchagent_mode_is_unsafe(self) -> None:
        self.service_plist.chmod(0o644)
        result = self.run_start("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mode", result.stderr)
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())
        self.assertFalse((self.state / "starts.log").exists())

    def test_start_fails_closed_when_current_launchagent_is_symlink(self) -> None:
        target = self.service_plist.with_name("foreign.plist")
        target.write_bytes(self.service_plist.read_bytes())
        self.service_plist.unlink()
        self.service_plist.symlink_to(target)
        result = self.run_start("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())
        self.assertFalse((self.state / "starts.log").exists())

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

    def test_stop_waits_for_supervisor_after_listener_disappears(self) -> None:
        (self.state / "loaded").write_text("")
        launchctl = self.bin / "launchctl"
        source = launchctl.read_text()
        source = source.replace(
            'echo "state = not running"',
            'if [[ ! -f "$state/quiescent" ]]; then echo "state = running"; echo "pid = 12345"; else echo "state = not running"; fi',
        )
        launchctl.write_text(source)
        process = subprocess.Popen(
            [str(self.repo / "start.sh"), "stop"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.2)
        self.assertIsNone(process.poll(), "stop must wait while the supervisor is still running")
        (self.state / "quiescent").touch()
        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn("STOPPED", output)


    def test_native_supervisor_owns_exact_process_group_and_bounded_shutdown(self) -> None:
        source = (ROOT / "macos/Sources/AgentRuntimeRuntimeService/main.swift").read_text()
        self.assertIn("posix_spawn", source)
        self.assertIn("POSIX_SPAWN_SETPGROUP", source)
        self.assertIn("posix_spawnattr_setpgroup", source)
        self.assertIn("waitpid", source)
        self.assertIn("SIGTERM", source)
        self.assertIn("SIGKILL", source)
        self.assertIn("kill(-childPGID", source)
        self.assertIn("childPID", source)
        self.assertIn("childPGID", source)
        self.assertNotIn("let child = Process()", source)
        self.assertNotIn("killall", source)
        self.assertNotIn("pkill", source)
        self.assertNotIn("pgrep", source)
        self.assertNotIn("protected-runtime-running", source)
        self.assertNotIn("SuccessfulExit", source)


if __name__ == "__main__":
    unittest.main()
