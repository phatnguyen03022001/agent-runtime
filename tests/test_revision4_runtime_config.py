from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Revision4RuntimeConfigTests(unittest.TestCase):
    def test_env_example_declares_tunnel_id_placeholder(self) -> None:
        text = (ROOT / ".env.example").read_text()
        self.assertIn("CONTROL_PLANE_API_KEY=", text)
        self.assertIn("CONTROL_PLANE_TUNNEL_ID=", text)
        self.assertIn("AGENT_RUNTIME_WORKSPACE_ROOT=", text)

    def _serve_fixture(self, temp: Path):
        repo = temp / "repo"
        home = temp / "home"
        tools = temp / "tools"
        repo.mkdir(); home.mkdir(); tools.mkdir()
        shutil.copy2(ROOT / "start.sh", repo / "start.sh")
        (repo / "start.sh").chmod(0o700)
        (repo / "agent_runtime").mkdir()
        (repo / "agent_runtime/server.py").write_text("# fixture runtime payload\n")
        (repo / ".venv/bin").mkdir(parents=True)
        (repo / ".venv/bin/python").write_text("#!/bin/sh\nexit 0\n")
        (repo / ".venv/bin/python").chmod(0o700)
        env_file = repo / ".env"
        env_file.write_text(
            f"CONTROL_PLANE_API_KEY=test-key\n"
            f"CONTROL_PLANE_TUNNEL_ID=stable-test-id\n"
            f"AGENT_RUNTIME_WORKSPACE_ROOT={temp}\n"
        )
        env_file.chmod(0o600)
        desired = home / "Library/Application Support/Agent Runtime/protected-runtime-running"
        desired.parent.mkdir(parents=True)
        desired.write_text("")
        capture = temp / "capture.log"
        tunnel = tools / "tunnel-client"
        tunnel.write_text(
            "#!/bin/bash\n"
            f"printf 'argv=%s\\n' \"$*\" >> {str(capture)!r}\n"
            f"for key in CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID TUNNEL_CLIENT_CONFIG "
            "TUNNEL_CLIENT_PROFILE TUNNEL_CLIENT_PROFILE_FILE TUNNEL_CLIENT_PROFILE_DIR "
            "XDG_CONFIG_HOME AGENT_RUNTIME_TUNNEL_PROFILE; do "
            f"printf 'env:%s=%s\\n' \"$key\" \"${{!key-}}\" >> {str(capture)!r}; done\n"
            "exit 0\n"
        )
        tunnel.chmod(0o700)
        return repo, home, env_file, tunnel, capture
    def test_serve_uses_env_owned_identity_without_profile_under_sterile_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, env_file, tunnel, capture = self._serve_fixture(Path(raw))
            before = hashlib.sha256(env_file.read_bytes()).digest()
            env = {
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "CONTROL_PLANE_TUNNEL_ID": "ambient-wrong-id",
                "TUNNEL_CLIENT_PROFILE_FILE": "/tmp/wrong.yaml",
                "AGENT_RUNTIME_TUNNEL_PROFILE": "wrong-profile",
            }
            result = subprocess.run(
                [str(repo / "start.sh"), "--serve", str(tunnel)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hashlib.sha256(env_file.read_bytes()).digest(), before)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())
            lines = capture.read_text().splitlines()
            self.assertEqual(sum(line == "env:CONTROL_PLANE_TUNNEL_ID=stable-test-id" for line in lines), 2)
            self.assertEqual(sum(line == "env:CONTROL_PLANE_API_KEY=test-key" for line in lines), 2)
            for line in lines:
                self.assertNotIn("--profile", line)
                if line.startswith("env:TUNNEL_CLIENT_") or line.startswith("env:AGENT_RUNTIME_TUNNEL_PROFILE"):
                    self.assertTrue(line.endswith("="), line)
            argv_lines = [line for line in lines if line.startswith("argv=")]
            self.assertEqual(len(argv_lines), 2)
            for line in argv_lines:
                self.assertIn("--control-plane.poll-channel main", line)
                self.assertIn("--health.listen-addr 127.0.0.1:8080", line)
                self.assertIn("-m agent_runtime.server,channel=main", line)
            self.assertTrue(argv_lines[0].startswith("argv=doctor "))
            self.assertIn("--explain", argv_lines[0])
            self.assertTrue(argv_lines[1].startswith("argv=run "))

    def test_serve_fails_closed_if_legacy_profile_reappears(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, _env_file, tunnel, capture = self._serve_fixture(Path(raw))
            profile = home / ".config/tunnel-client/agent-runtime.yaml"
            profile.parent.mkdir(parents=True)
            profile.write_text("control_plane:\n  tunnel_id: forbidden\n")
            result = subprocess.run(
                [str(repo / "start.sh"), "--serve", str(tunnel)],
                env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(capture.exists())
    def _start_fixture(self, temp: Path, *, health_ok: bool):
        repo = temp / "repo"
        home = temp / "home"
        bin_dir = temp / "bin"
        state = temp / "state"
        for path in (repo, home, bin_dir, state):
            path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "start.sh", repo / "start.sh")
        (repo / "start.sh").chmod(0o700)
        (repo / "agent_runtime").mkdir()
        (repo / "agent_runtime/server.py").write_text("# fixture runtime payload\n")
        plist = home / "Library/LaunchAgents/com.picmao.agent-runtime-runtime.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("fixture\n")
        command = (
            "/opt/homebrew/bin/tunnel-client run "
            "--control-plane.poll-channel main "
            f"--mcp.command command={repo.resolve()}/.venv/bin/python -m agent_runtime.server,channel=main "
            "--health.listen-addr 127.0.0.1:8080"
        )

        def write_exe(name: str, text: str) -> None:
            path = bin_dir / name
            path.write_text(text)
            path.chmod(0o700)
        write_exe(
            "launchctl",
            f'''#!/bin/bash
set -e
case "$1" in
  print) exit 0 ;;
  bootstrap) exit 0 ;;
  kickstart) touch {str(state / "kicked")!r}; exit 0 ;;
  kill) rm -f {str(state / "kicked")!r}; exit 0 ;;
  *) exit 2 ;;
esac
''',
        )
        write_exe(
            "lsof",
            f'''#!/bin/bash
if [[ -f {str(state / "kicked")!r} ]]; then echo 4242; fi
''',
        )
        write_exe(
            "ps",
            f'''#!/bin/bash
if [[ "$*" == *"comm="* ]]; then
  echo /opt/homebrew/bin/tunnel-client
elif [[ "$*" == *"command="* ]]; then
  echo {command!r}
fi
''',
        )
        write_exe(
            "curl",
            f'''#!/bin/bash
printf '%s\n' "$*" >> {str(state / "curl.log")!r}
[[ {"1" if health_ok else "0"} == 1 ]]
''',
        )
        write_exe("sleep", "#!/bin/sh\nexit 0\n")
        env = {
            "HOME": str(home),
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        return repo, home, state, env

    def test_start_returns_success_only_after_singleton_health_and_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, _home, state, env = self._start_fixture(Path(raw), health_ok=True)
            result = subprocess.run(
                [str(repo / "start.sh"), "start"], env=env,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Agent Runtime desired state: RUNNING", result.stdout)
            probes = (state / "curl.log").read_text()
            self.assertIn("http://127.0.0.1:8080/healthz", probes)
            self.assertIn("http://127.0.0.1:8080/readyz", probes)

    def test_start_failure_is_nonzero_and_never_reports_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, _home, state, env = self._start_fixture(Path(raw), health_ok=False)
            result = subprocess.run(
                [str(repo / "start.sh"), "start"], env=env,
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Agent Runtime desired state: RUNNING", result.stdout)
            self.assertIn("ready", result.stderr.lower())
            self.assertTrue((state / "curl.log").exists())

    def test_start_script_has_no_profile_runtime_architecture(self) -> None:
        text = (ROOT / "start.sh").read_text()
        self.assertNotIn("--profile-file", text)
        self.assertNotIn("TUNNEL_CLIENT_PROFILE_FILE", text)
        self.assertNotIn("AGENT_RUNTIME_TUNNEL_PROFILE", text)
        self.assertIn("CONTROL_PLANE_TUNNEL_ID", text)
        self.assertIn("--control-plane.poll-channel", text)
        self.assertIn("--mcp.command", text)
        self.assertIn("--health.listen-addr", text)


if __name__ == "__main__":
    unittest.main()
