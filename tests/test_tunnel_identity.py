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
        self._write(
            repo / ".env",
            f"CONTROL_PLANE_API_KEY=test-key\nAGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n",
        )
        profile = self._profile(home)
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
        return subprocess.run([str(repo / "start.sh")], cwd=repo, env=env, text=True, capture_output=True, check=False)

    def test_start_uses_exact_canonical_profile_and_sanitized_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            result = self._run_start(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = capture.read_text().splitlines()
            profile = home / ".config" / "tunnel-client" / "agent-runtime.yaml"
            self.assertEqual([line for line in lines if line.startswith("argv=")], [
                f"argv=doctor --profile-file {profile} --explain",
                f"argv=run --profile-file {profile}",
            ])
            forbidden = ("CONTROL_PLANE_TUNNEL_ID", "TUNNEL_CLIENT_CONFIG", "TUNNEL_CLIENT_PROFILE=", "TUNNEL_CLIENT_PROFILE_FILE", "TUNNEL_CLIENT_PROFILE_DIR", "XDG_CONFIG_HOME", "AGENT_RUNTIME_TUNNEL_PROFILE")
            for item in forbidden:
                self.assertFalse(any(item in line for line in lines if line.startswith("env:")), item)
            self.assertEqual(sum(line == "env:CONTROL_PLANE_API_KEY=test-key" for line in lines), 2)

    def test_repeated_start_does_not_mutate_canonical_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            profile = home / ".config" / "tunnel-client" / "agent-runtime.yaml"
            before = hashlib.sha256(profile.read_bytes()).digest()
            for _ in range(2):
                result = self._run_start(repo, home, bin_dir, capture)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hashlib.sha256(profile.read_bytes()).digest(), before)


    def test_start_matching_legacy_identity_converges_but_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            env_file = repo / ".env"
            env_file.write_text(env_file.read_text() + "CONTROL_PLANE_TUNNEL_ID=tunnel_profile\nAGENT_RUNTIME_TUNNEL_PROFILE=legacy\n")
            result = self._run_start(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("CONTROL_PLANE_TUNNEL_ID=", env_file.read_text())
            self.assertNotIn("AGENT_RUNTIME_TUNNEL_PROFILE=", env_file.read_text())
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._start_fixture(Path(raw))
            env_file = repo / ".env"
            env_file.write_text(env_file.read_text() + "CONTROL_PLANE_TUNNEL_ID=legacy-wrong\n")
            profile = home / ".config/tunnel-client/agent-runtime.yaml"
            before = profile.read_bytes()
            result = self._run_start(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("migration", result.stderr.lower())
            self.assertEqual(profile.read_bytes(), before)

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
        self._write(package, r'''#!/usr/bin/env bash
set -euo pipefail
APP="$PWD/build/Agent Runtime.app"
mkdir -p "$APP/Contents/MacOS"
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
/usr/bin/codesign --force --sign - "$APP" >/dev/null 2>&1
''', 0o700)
        self._fake_tunnel_client(bin_dir)
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
        lines += ["AGENT_RUNTIME_TUNNEL_PROFILE=legacy-profile", f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent}"]
        self._write(path, "\n".join(lines) + "\n")
        return path

    def test_install_matching_legacy_identity_converges_and_preserves_profile_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            profile = self._profile(home, "same-id")
            before = profile.read_bytes()
            env_file = self._env(repo, "same-id")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(profile.read_bytes(), before)
            text = env_file.read_text()
            self.assertNotIn("CONTROL_PLANE_TUNNEL_ID=", text)
            self.assertNotIn("AGENT_RUNTIME_TUNNEL_PROFILE=", text)
            self.assertNotIn("argv=init ", capture.read_text())

    def test_install_mismatched_legacy_identity_fails_closed_without_profile_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            profile = self._profile(home, "profile-id")
            before = profile.read_bytes()
            self._env(repo, "legacy-id")
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("migration", result.stderr.lower())
            self.assertEqual(profile.read_bytes(), before)
            self.assertFalse(capture.exists() and "argv=init " in capture.read_text())

    def test_install_missing_profile_bootstraps_once_from_one_unambiguous_legacy_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            env_file = self._env(repo, "bootstrap-id")
            first = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(first.returncode, 0, first.stderr)
            profile = home / ".config" / "tunnel-client" / "agent-runtime.yaml"
            first_bytes = profile.read_bytes()
            self.assertIn("tunnel_id: bootstrap-id", first_bytes.decode())
            self.assertNotIn("CONTROL_PLANE_TUNNEL_ID=", env_file.read_text())
            second = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(profile.read_bytes(), first_bytes)
            self.assertEqual(capture.read_text().count("argv=init "), 1)

    def test_install_missing_profile_rejects_absent_or_ambiguous_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, None)
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, "legacy-id")
            result = self._run_install(repo, home, bin_dir, capture, tunnel_id="operator-id")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ambiguous", result.stderr.lower())
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())

    def test_install_rejects_malformed_symlink_or_nonregular_profile(self) -> None:
        for state in ("malformed", "symlink", "directory"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as raw:
                repo, home, bin_dir, capture = self._install_fixture(Path(raw))
                profile = home / ".config/tunnel-client/agent-runtime.yaml"
                profile.parent.mkdir(parents=True)
                if state == "malformed": self._write(profile, "control_plane:\n  tunnel_id:\n")
                elif state == "symlink":
                    target = home / "target.yaml"; self._write(target, "control_plane:\n  tunnel_id: x\n"); profile.symlink_to(target)
                else: profile.mkdir()
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
