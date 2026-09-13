from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TunnelIdentityTests(unittest.TestCase):
    def _write(self, path: Path, text: str, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def _profile(self, home: Path, tunnel_id: str = "tunnel_profile") -> Path:
        path = home / ".config" / "tunnel-client" / "agent-runtime.yaml"
        self._write(
            path,
            textwrap.dedent(
                f"""\
                control_plane:
                  api_key: env:CONTROL_PLANE_API_KEY
                  tunnel_id: {tunnel_id}
                mcp:
                  command:
                    - command: /tmp/python -m agent_runtime.server
                """
            ),
        )
        return path

    def _fake_tunnel_client(self, bin_dir: Path) -> Path:
        tool = bin_dir / "tunnel-client"
        self._write(
            tool,
            r'''#!/usr/bin/env bash
set -euo pipefail
CAPTURE="$(dirname "$0")/capture.log"
printf 'argv=%s\n' "$*" >> "$CAPTURE"
for key in CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID TUNNEL_CLIENT_CONFIG TUNNEL_CLIENT_PROFILE TUNNEL_CLIENT_PROFILE_FILE TUNNEL_CLIENT_PROFILE_DIR XDG_CONFIG_HOME AGENT_RUNTIME_TUNNEL_PROFILE AGENT_RUNTIME_WORKSPACE_ROOT; do
  if [[ -n "${!key-}" ]]; then printf 'env:%s=%s\n' "$key" "${!key}" >> "$CAPTURE"; fi
done
if [[ "${1-}" == init ]]; then
  profile=agent-runtime
  profile_dir="${HOME}/.config/tunnel-client"
  tunnel_id=""
  shift
  while (($#)); do
    case "$1" in
      --profile) profile="$2"; shift 2 ;;
      --profile-dir) profile_dir="$2"; shift 2 ;;
      --tunnel-id) tunnel_id="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  mkdir -p "$profile_dir"
  cat > "$profile_dir/$profile.yaml" <<EOF
control_plane:
  api_key: env:CONTROL_PLANE_API_KEY
  tunnel_id: $tunnel_id
mcp:
  command:
    - command: /tmp/python -m agent_runtime.server
EOF
fi
''',
            0o700,
        )
        return tool

    def _start_fixture(self, temp: Path) -> tuple[Path, Path, Path, Path]:
        repo = temp / "repo"
        home = temp / "home"
        workspace = temp / "workspace"
        bin_dir = temp / "bin"
        repo.mkdir(); home.mkdir(); workspace.mkdir(); bin_dir.mkdir()
        shutil.copy2(ROOT / "start.sh", repo / "start.sh")
        (repo / "start.sh").chmod(0o700)
        (repo / "agent_runtime").mkdir()
        (repo / "agent_runtime/server.py").write_text("# fixture runtime payload\n")
        self._write(repo / ".venv/bin/python", "#!/bin/sh\nexit 0\n", 0o700)
        self._write(
            repo / ".env",
            f"CONTROL_PLANE_API_KEY=test-key\nCONTROL_PLANE_TUNNEL_ID=stable-test-id\nAGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n",
        )
        self._fake_tunnel_client(bin_dir)
        capture = bin_dir / "capture.log"
        return repo, home, bin_dir, capture

    def _run_start(self, repo: Path, home: Path, bin_dir: Path, capture: Path) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "USER": "fixture",
            "TMPDIR": str(home / "tmp"),
            "LANG": "C",
            "CONTROL_PLANE_API_KEY": "ambient-wrong-key",
            "CONTROL_PLANE_TUNNEL_ID": "ambient-wrong-id",
            "TUNNEL_CLIENT_CONFIG": "/tmp/wrong-config.yaml",
            "TUNNEL_CLIENT_PROFILE": "wrong-profile",
            "TUNNEL_CLIENT_PROFILE_FILE": "/tmp/wrong-profile.yaml",
            "TUNNEL_CLIENT_PROFILE_DIR": "/tmp/wrong-dir",
            "XDG_CONFIG_HOME": "/tmp/wrong-xdg",
            "AGENT_RUNTIME_TUNNEL_PROFILE": "wrong-agent-profile",
        }
        desired = home / "Library" / "Application Support" / "Agent Runtime" / "protected-runtime-running"
        desired.parent.mkdir(parents=True, exist_ok=True)
        desired.write_text("")
        return subprocess.run([str(repo / "start.sh"), "--serve", str(bin_dir / "tunnel-client")], cwd=repo, env=env, text=True, capture_output=True, check=False)

    def test_start_uses_env_identity_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            result = self._run_start(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = capture.read_text().splitlines()
            resolved_repo = repo.resolve()
            self.assertEqual([line for line in lines if line.startswith("argv=")], [
                f"argv=doctor --control-plane.poll-channel main --mcp.command command={resolved_repo}/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080 --explain",
                f"argv=run --control-plane.poll-channel main --mcp.command command={resolved_repo}/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080",
            ])
            self.assertEqual(sum(line == "env:CONTROL_PLANE_API_KEY=test-key" for line in lines), 2)
            self.assertEqual(sum(line == "env:CONTROL_PLANE_TUNNEL_ID=stable-test-id" for line in lines), 2)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())

    def test_repeated_start_preserves_env_bytes_and_never_creates_legacy_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            env_file = repo / ".env"
            before = hashlib.sha256(env_file.read_bytes()).digest()
            for _ in range(2):
                result = self._run_start(repo, home, bin_dir, capture)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hashlib.sha256(env_file.read_bytes()).digest(), before)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())


    def test_start_rejects_duplicate_or_malformed_env_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            env_file = repo / ".env"
            env_file.write_text(env_file.read_text() + "CONTROL_PLANE_TUNNEL_ID=duplicate\n")
            result = self._run_start(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate", result.stderr.lower())
            self.assertFalse(capture.exists())
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            env_file = repo / ".env"
            env_file.write_text("not-an-env-entry\n")
            result = self._run_start(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("malformed", result.stderr.lower())
            self.assertFalse(capture.exists())

    def _install_fixture(self, temp: Path) -> tuple[Path, Path, Path, Path]:
        repo = temp / "agent-runtime"
        home = temp / "home"
        bin_dir = temp / "bin"
        capture = bin_dir / "capture.log"
        repo.mkdir(); home.mkdir(); bin_dir.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "dev", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:phatnguyen03022001/agent-runtime.git"], check=True)
        shutil.copy2(ROOT / "install.sh", repo / "install.sh")
        (repo / "install.sh").chmod(0o700)
        shutil.copy2(ROOT / ".env.example", repo / ".env.example")
        self._write(repo / "requirements.txt", "")
        self._write(repo / "verify", "#!/usr/bin/env bash\nexit 0\n", 0o700)
        venv_python = repo / ".venv" / "bin" / "python"
        self._write(venv_python, "#!/usr/bin/env bash\nexit 0\n", 0o700)
        package = repo / "macos" / "package_app.sh"
        config_helper = repo / "macos" / "runtime_config.py"
        config_helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "macos/runtime_config.py", config_helper)
        self._write(package, r'''#!/usr/bin/env bash
set -euo pipefail
APP="$PWD/build/Agent Runtime.app"
RUNTIME="$APP/Contents/Resources/runtime"
mkdir -p "$APP/Contents/MacOS" "$RUNTIME/agent_runtime" "$RUNTIME/.venv/bin"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.picmao.agent-runtime</string>
<key>CFBundleExecutable</key><string>AgentRuntimeMenuBar</string>
</dict></plist>
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$APP/Contents/MacOS/AgentRuntimeMenuBar"
chmod +x "$APP/Contents/MacOS/AgentRuntimeMenuBar"
printf '#!/usr/bin/env bash\nexit 0\n' > "$RUNTIME/start.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$RUNTIME/.venv/bin/python"
printf '# fixture runtime payload\n' > "$RUNTIME/agent_runtime/server.py"
chmod +x "$RUNTIME/start.sh" "$RUNTIME/.venv/bin/python"
START_SHA="$(shasum -a 256 "$RUNTIME/start.sh" | awk '{print $1}')"
SERVER_SHA="$(shasum -a 256 "$RUNTIME/agent_runtime/server.py" | awk '{print $1}')"
printf '{"schema":1,"owner":"com.picmao.agent-runtime","runtime_revision":"0000000000000000000000000000000000000000","entrypoint":"runtime/start.sh","python":"runtime/.venv/bin/python","mcp_package":"runtime/agent_runtime","start_sha256":"%s","server_sha256":"%s"}\n' "$START_SHA" "$SERVER_SHA" > "$APP/Contents/Resources/runtime-manifest.json"
/usr/bin/codesign --force --sign - "$APP" >/dev/null 2>&1
''', 0o700)
        self._fake_tunnel_client(bin_dir)
        self._write(
            bin_dir / "launchctl",
            r'''#!/usr/bin/env bash
set -euo pipefail
STATE="$(dirname "$0")/launchd-loaded"
case "${1-}" in
  print) [[ -f "$STATE" ]] ;;
  bootstrap) : > "$STATE" ;;
  *) exit 2 ;;
esac
''',
            0o700,
        )
        return repo, home, bin_dir, capture

    def _run_install(self, repo: Path, home: Path, bin_dir: Path, capture: Path, *, tunnel_id: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({"HOME": str(home), "PATH": f"{bin_dir}:{env['PATH']}"})
        for key in ("CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID", "TUNNEL_CLIENT_CONFIG", "TUNNEL_CLIENT_PROFILE", "TUNNEL_CLIENT_PROFILE_FILE", "TUNNEL_CLIENT_PROFILE_DIR", "XDG_CONFIG_HOME"):
            env.pop(key, None)
        if tunnel_id is not None:
            env["CONTROL_PLANE_TUNNEL_ID"] = tunnel_id
        return subprocess.run([str(repo / "install.sh")], cwd=repo, env=env, text=True, capture_output=True, check=False)

    def _env(self, repo: Path, tunnel_id: str | None = None, api_key: str = "test-key") -> Path:
        path = repo / ".env"
        lines = [f"CONTROL_PLANE_API_KEY={api_key}"]
        if tunnel_id is not None: lines.append(f"CONTROL_PLANE_TUNNEL_ID={tunnel_id}")
        lines += [f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent}"]
        self._write(path, "\n".join(lines) + "\n")
        return path

    def test_install_first_bootstrap_persists_derived_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            source = self._env(repo, "bootstrap-id")
            source.write_text(source.read_text().replace(f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent}", "AGENT_RUNTIME_WORKSPACE_ROOT="))

            result = self._run_install(repo, home, bin_dir, capture)

            self.assertEqual(result.returncode, 0, result.stderr)
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            self.assertIn(f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent.resolve()}\n", canonical.read_text())

    def test_install_preserves_env_owned_identity_and_registers_absolute_runtime_binary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            env_file = self._env(repo, "same-id")
            with env_file.open("a") as handle:
                handle.write("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=22\n")
            source_before = env_file.read_bytes()
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = env_file.read_text()
            self.assertIn("CONTROL_PLANE_TUNNEL_ID=same-id", text)
            self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=22", text)
            self.assertNotIn("AGENT_RUNTIME_TUNNEL_PROFILE=", text)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())
            self.assertIn("argv=doctor --control-plane.poll-channel main", capture.read_text())
            plist = (home / "Library/LaunchAgents/com.picmao.agent-runtime-runtime.plist").read_text()
            self.assertIn(str(bin_dir / "tunnel-client"), plist)
            self.assertIn("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", plist)
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            self.assertEqual(canonical.stat().st_mode & 0o777, 0o600)
            self.assertEqual(env_file.read_bytes(), source_before)
            canonical_text = canonical.read_text()
            self.assertIn(f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent.resolve()}\n", canonical_text)
            self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=22\n", canonical_text)

    def test_install_accepts_a_verified_homebrew_style_tunnel_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            client = bin_dir / "tunnel-client"
            target = bin_dir / "tunnel-client-cellar"
            client.rename(target)
            client.symlink_to(target.name)
            self._env(repo, "same-id")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(client), (home / "Library/LaunchAgents/com.picmao.agent-runtime-runtime.plist").read_text())

    def test_install_rejects_reappeared_legacy_configuration_without_starting_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            legacy = self._profile(home, "forbidden")
            before = legacy.read_bytes()
            self._env(repo, "stable-id")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("legacy", result.stderr.lower())
            self.assertEqual(legacy.read_bytes(), before)
            self.assertFalse(capture.exists())

    def test_install_is_idempotent_without_legacy_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            env_file = self._env(repo, "bootstrap-id")
            first = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_bytes = env_file.read_bytes()
            self.assertIn(b"CONTROL_PLANE_TUNNEL_ID=bootstrap-id", first_bytes)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())
            second = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(env_file.read_bytes(), first_bytes)
            self.assertEqual(capture.read_text().count("argv=doctor "), 2)

    def test_install_preserves_existing_canonical_config_and_rejects_bad_mode_or_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            source = self._env(repo, "source-id")
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes(source.read_bytes().replace(b"source-id", b"canonical-id"))
            canonical.chmod(0o600)
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"canonical-id", canonical.read_bytes())
            self.assertNotIn(b"source-id", canonical.read_bytes())
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, "mode-id")
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            canonical.parent.mkdir(parents=True)
            canonical.write_bytes((repo / ".env").read_bytes())
            canonical.chmod(0o644)
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("0600", result.stderr)
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, "symlink-id")
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            canonical.parent.mkdir(parents=True)
            canonical.symlink_to(repo / ".env")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("regular non-symlink", result.stderr)

    def test_install_rejects_missing_or_duplicate_env_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, None)
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, "legacy-id")
            with (repo / ".env").open("a") as handle:
                handle.write("CONTROL_PLANE_TUNNEL_ID=duplicate\n")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate", result.stderr.lower())
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())

    def test_install_rejects_legacy_path_in_every_form(self) -> None:
        for state in ("malformed", "symlink", "directory"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as raw:
                repo, home, bin_dir, capture = self._install_fixture(Path(raw))
                legacy = home / ".config/tunnel-client/agent-runtime.yaml"
                legacy.parent.mkdir(parents=True)
                if state == "malformed": self._write(legacy, "legacy\n")
                elif state == "symlink":
                    target = home / "target.yaml"; self._write(target, "legacy\n"); legacy.symlink_to(target)
                else: legacy.mkdir()
                self._env(repo, "x")
                result = self._run_install(repo, home, bin_dir, capture)
                self.assertNotEqual(result.returncode, 0)

    def test_install_missing_api_key_never_mutates_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, "bootstrap-id", api_key="")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())


if __name__ == "__main__":
    unittest.main()
