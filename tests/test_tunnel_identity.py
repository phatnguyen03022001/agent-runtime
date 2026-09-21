from __future__ import annotations

import hashlib
import json
import os
import plistlib
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
for key in CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID TUNNEL_CLIENT_CONFIG TUNNEL_CLIENT_PROFILE TUNNEL_CLIENT_PROFILE_FILE TUNNEL_CLIENT_PROFILE_DIR XDG_CONFIG_HOME AGENT_RUNTIME_TUNNEL_PROFILE AGENT_RUNTIME_WORKSPACE_ROOT AGENT_RUNTIME_MAX_PARALLELISM; do
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
            f"CONTROL_PLANE_API_KEY=test-key\nCONTROL_PLANE_TUNNEL_ID=stable-test-id\nAGENT_RUNTIME_WORKSPACE_ROOT={workspace}\nAGENT_RUNTIME_MAX_PARALLELISM=10\n",
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
            self.assertEqual(sum(line == "env:AGENT_RUNTIME_MAX_PARALLELISM=10" for line in lines), 2)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())

    def test_start_rejects_invalid_parallelism_limit_instead_of_clamping(self) -> None:
        for configured in ("", "0", "11", "-1", "2.0", "many"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as raw:
                repo, home, bin_dir, capture = self._start_fixture(Path(raw))
                env_file = repo / ".env"
                text = env_file.read_text().replace("AGENT_RUNTIME_MAX_PARALLELISM=10", f"AGENT_RUNTIME_MAX_PARALLELISM={configured}")
                env_file.write_text(text)
                result = self._run_start(repo, home, bin_dir, capture)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AGENT_RUNTIME_MAX_PARALLELISM", result.stderr)
                self.assertFalse(capture.exists())

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
        self._write(repo / "requirements.txt", "mcp==2.2.0\n")
        shutil.copy2(ROOT / "requirements.lock", repo / "requirements.lock")
        self._write(repo / "verify", "#!/usr/bin/env bash\nexit 0\n", 0o700)
        venv_python = repo / ".venv" / "bin" / "python"
        canonical_python = (
            "#!/usr/bin/env bash\n"
            "if [[ \"${1-}\" == \"-c\" ]]; then\n"
            "  printf '%s\\n' $'cpython\\t3.13.13\\tcpython-313\\tcpython-313-darwin\\tdarwin\\tarm64\\t/fake/python3.13'\n"
            "fi\n"
            "exit 0\n"
        )
        self._write(venv_python, canonical_python, 0o700)
        self._write(bin_dir / "python3.13", canonical_python, 0o700)
        package = repo / "macos" / "package_app.sh"
        config_helper = repo / "macos" / "runtime_config.py"
        config_helper.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "macos/runtime_config.py", config_helper)
        provenance_copy = repo / "macos" / "package_provenance.py"
        shutil.copy2(ROOT / "macos/package_provenance.py", provenance_copy)
        provenance_source = provenance_copy.read_text()
        team_reader = "def _codesign_team_identifier(path: Path) -> str:\n"
        if team_reader not in provenance_source:
            raise AssertionError("package provenance TeamIdentifier reader fixture hook is unavailable")
        provenance_source = provenance_source.replace(
            team_reader,
            team_reader
            + '    fixture_team = os.environ.get("AGENT_RUNTIME_TEST_TEAM_IDENTIFIER")\n'
            + '    if fixture_team:\n'
            + '        return fixture_team\n',
            1,
        )
        verify_reader = "def _verify_codesign(app: Path) -> None:\n"
        if verify_reader not in provenance_source:
            raise AssertionError("package provenance codesign verifier fixture hook is unavailable")
        provenance_source = provenance_source.replace(
            verify_reader,
            verify_reader
            + '    if os.environ.get("AGENT_RUNTIME_TEST_SKIP_CODESIGN_VERIFY") == "1":\n'
            + '        return\n',
            1,
        )
        provenance_copy.write_text(provenance_source)
        shutil.copy2(ROOT / "macos/packaging_python.sh", repo / "macos" / "packaging_python.sh")
        shutil.copy2(ROOT / "macos/candidate_cutover.py", repo / "macos" / "candidate_cutover.py")
        self._write(package, r'''#!/usr/bin/env bash
set -euo pipefail
BUILD_ROOT="$PWD/build"
CANDIDATES_ROOT="$BUILD_ROOT/candidates"
STAGE="$BUILD_ROOT/.fixture-package.$$"
APP="$STAGE/Agent Runtime.app"
HANDOFF="$STAGE/Agent Runtime.candidate.json"
RUNTIME="$APP/Contents/Resources/runtime"
SERVICE_DIR="$APP/Contents/Library/LaunchAgents"
mkdir -p "$APP/Contents/MacOS" "$SERVICE_DIR" "$RUNTIME/agent_runtime" "$RUNTIME/.venv/bin"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.picmao.agent-runtime</string>
<key>CFBundleExecutable</key><string>AgentRuntimeMenuBar</string>
</dict></plist>
EOF
cat > "$APP/Contents/MacOS/AgentRuntimeMenuBar" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1-}" == "--service-management" ]] || exit 0
STATE="$HOME/Library/Application Support/Agent Runtime/test-service-state.json"
LAUNCHCTL_BIN="$(command -v launchctl)"
FAKE_BIN="$(dirname "$LAUNCHCTL_BIN")"
LOADED="$FAKE_BIN/launchd-loaded"
PROGRAMS="$FAKE_BIN/launchd-programs"
MACOS_DIR="${0%/*}"
CONTENTS_DIR="${MACOS_DIR%/*}"
APP_ROOT="${CONTENTS_DIR%/*}"
SERVICE="gui/$(id -u)/com.picmao.agent-runtime-runtime-service"
PROGRAM="$APP_ROOT/Contents/MacOS/AgentRuntimeRuntimeService"
mkdir -p "$(dirname "$STATE")"
update_state() {
  /usr/bin/python3 - "$STATE" "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
path, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
state = {"main_app": "not-registered", "runtime_agent": "not-registered"}
if path.is_file(): state.update(json.loads(path.read_text()))
state[key] = value
path.write_text(json.dumps(state, sort_keys=True) + "\n")
print(json.dumps(state, sort_keys=True))
PY
}
load_runtime() {
  touch "$LOADED"
  mkdir -p "$PROGRAMS"
  grep -Fxq -- "$SERVICE" "$LOADED" || printf '%s\n' "$SERVICE" >> "$LOADED"
  printf '%s\n' "$PROGRAM" > "$PROGRAMS/com.picmao.agent-runtime-runtime-service"
}
unload_runtime() {
  if [[ -f "$LOADED" ]]; then
    tmp="$LOADED.tmp"
    grep -Fvx -- "$SERVICE" "$LOADED" > "$tmp" || true
    mv "$tmp" "$LOADED"
  fi
  rm -f "$PROGRAMS/com.picmao.agent-runtime-runtime-service"
}
case "${2-}" in
  status)
    if [[ -f "$STATE" ]]; then cat "$STATE"; else printf '%s\n' '{"main_app":"not-registered","runtime_agent":"not-registered"}'; fi
    ;;
  register-main) update_state main_app enabled ;;
  register-runtime) load_runtime; update_state runtime_agent enabled ;;
  unregister-main) update_state main_app not-registered ;;
  unregister-runtime) unload_runtime; update_state runtime_agent not-registered ;;
  register)
    load_runtime
    printf '%s\n' '{"main_app":"enabled","runtime_agent":"enabled"}' > "$STATE"
    cat "$STATE"
    ;;
  unregister)
    unload_runtime
    printf '%s\n' '{"main_app":"not-registered","runtime_agent":"not-registered"}' > "$STATE"
    cat "$STATE"
    ;;
  *) exit 2 ;;
esac
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$APP/Contents/MacOS/AgentRuntimeRuntimeService"
printf '#!/usr/bin/env bash\nexit 0\n' > "$APP/Contents/MacOS/AgentRuntimeScreenCapture"
chmod +x "$APP/Contents/MacOS/AgentRuntimeMenuBar" "$APP/Contents/MacOS/AgentRuntimeRuntimeService" "$APP/Contents/MacOS/AgentRuntimeScreenCapture"
cat > "$SERVICE_DIR/com.picmao.agent-runtime-runtime-service.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.picmao.agent-runtime-runtime-service</string>
<key>BundleProgram</key><string>Contents/MacOS/AgentRuntimeRuntimeService</string>
<key>RunAtLoad</key><false/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$RUNTIME/start.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$RUNTIME/.venv/bin/python"
printf '# fixture runtime payload\n' > "$RUNTIME/agent_runtime/server.py"
chmod +x "$RUNTIME/start.sh" "$RUNTIME/.venv/bin/python"
REVISION="$(git rev-parse HEAD)"
TREE="$(git rev-parse 'HEAD^{tree}')"
/usr/bin/python3 "$PWD/macos/package_provenance.py" manifest \
  "$RUNTIME" "$APP/Contents/Resources/runtime-manifest.json" \
  "$REVISION" "$TREE" "$PWD/requirements.lock"
printf '%s\n' "$$" > "$APP/Contents/Resources/fixture-build-id"
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1
/usr/bin/python3 "$PWD/macos/package_provenance.py" seal \
  "$APP" "$HANDOFF" >/dev/null
PUBLISHED="$(/usr/bin/python3 "$PWD/macos/package_provenance.py" publish \
  "$APP" "$HANDOFF" "$CANDIDATES_ROOT")"
IFS=$'\t' read -r FINAL_APP FINAL_HANDOFF CANDIDATE_SHA256 <<< "$PUBLISHED"
printf 'candidate_app=%s\n' "$FINAL_APP"
printf 'candidate_handoff=%s\n' "$FINAL_HANDOFF"
printf 'candidate_sha256=%s\n' "$CANDIDATE_SHA256"
''', 0o700)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
        self._fake_tunnel_client(bin_dir)
        self._write(
            bin_dir / "launchctl",
            r'''#!/usr/bin/env bash
set -euo pipefail
STATE="$(dirname "$0")/launchd-loaded"
PROGRAMS="$(dirname "$0")/launchd-programs"
case "${1-}" in
  print)
    if [[ ! -f "$STATE" ]] || ! grep -Fxq -- "$2" "$STATE"; then
      printf 'Could not find service "%s" in domain for user gui: 501\n' "${2##*/}" >&2
      exit 113
    fi
    label="${2##*/}"
    program="$(cat "$PROGRAMS/$label")"
    printf '%s = {\n\tpath = /fake/%s.plist\n\tstate = not running\n\tprogram = %s\n}\n' "$2" "$label" "$program"
    ;;
  bootstrap)
    label="$(basename "$3" .plist)"
    service="$2/$label"
    program="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$3")"
    touch "$STATE"
    mkdir -p "$PROGRAMS"
    grep -Fxq -- "$service" "$STATE" || printf '%s\n' "$service" >> "$STATE"
    printf '%s\n' "$program" > "$PROGRAMS/$label"
    ;;
  bootout)
    [[ -f "$STATE" ]] || exit 0
    tmp="$STATE.tmp"
    grep -Fvx -- "$2" "$STATE" > "$tmp" || true
    mv "$tmp" "$STATE"
    rm -f "$PROGRAMS/${2##*/}"
    ;;
  kickstart)
    service="${!#}"
    [[ -f "$STATE" ]] && grep -Fxq -- "$service" "$STATE"
    ;;
  *) exit 2 ;;
esac
''',
            0o700,
        )
        return repo, home, bin_dir, capture

    def _run_install(
        self,
        repo: Path,
        home: Path,
        bin_dir: Path,
        capture: Path,
        *,
        api_key: str | None = None,
        tunnel_id: str | None = None,
        args: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AGENT_RUNTIME_TEST_TEAM_IDENTIFIER": "TEAMTEST",
            "AGENT_RUNTIME_TEST_SKIP_CODESIGN_VERIFY": "1",
        })
        for key in ("CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID", "TUNNEL_CLIENT_CONFIG", "TUNNEL_CLIENT_PROFILE", "TUNNEL_CLIENT_PROFILE_FILE", "TUNNEL_CLIENT_PROFILE_DIR", "XDG_CONFIG_HOME"):
            env.pop(key, None)
        if api_key is not None:
            env["CONTROL_PLANE_API_KEY"] = api_key
        if tunnel_id is not None:
            env["CONTROL_PLANE_TUNNEL_ID"] = tunnel_id
        return subprocess.run(["/bin/bash", str(repo / "install.sh"), *args], cwd=repo, env=env, text=True, capture_output=True, check=False)

    def _env(self, repo: Path, tunnel_id: str | None = None, api_key: str = "test-key") -> Path:
        path = repo / ".env"
        lines = [f"CONTROL_PLANE_API_KEY={api_key}"]
        if tunnel_id is not None: lines.append(f"CONTROL_PLANE_TUNNEL_ID={tunnel_id}")
        lines += [f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent}"]
        self._write(path, "\n".join(lines) + "\n")
        return path

    def test_install_first_bootstrap_accepts_process_environment_credentials_and_keeps_source_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            result = self._run_install(
                repo, home, bin_dir, capture, api_key="env-key", tunnel_id="env-tunnel"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            source = repo / ".env"
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            canonical_text = canonical.read_text()
            self.assertIn("CONTROL_PLANE_API_KEY=env-key\n", canonical_text)
            self.assertIn("CONTROL_PLANE_TUNNEL_ID=env-tunnel\n", canonical_text)
            self.assertNotIn("env-key", result.stdout + result.stderr)
            self.assertNotIn("env-tunnel", result.stdout + result.stderr)

        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            self._env(repo, "source-tunnel", api_key="source-key")
            result = self._run_install(
                repo, home, bin_dir, capture, api_key="env-key", tunnel_id="env-tunnel"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            canonical_text = canonical.read_text()
            self.assertIn("CONTROL_PLANE_API_KEY=source-key\n", canonical_text)
            self.assertIn("CONTROL_PLANE_TUNNEL_ID=source-tunnel\n", canonical_text)
            self.assertNotIn("env-key", canonical_text)
            self.assertNotIn("env-tunnel", canonical_text)

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
                handle.write("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n")
            source_before = env_file.read_bytes()
            result = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = env_file.read_text()
            self.assertIn("CONTROL_PLANE_TUNNEL_ID=same-id", text)
            self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6", text)
            self.assertNotIn("AGENT_RUNTIME_TUNNEL_PROFILE=", text)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())
            self.assertIn("argv=doctor --control-plane.poll-channel main", capture.read_text())
            legacy_plist = home / "Library/LaunchAgents/com.picmao.agent-runtime-runtime.plist"
            self.assertFalse(legacy_plist.exists())
            bundled_plist = home / "Applications/Agent Runtime.app/Contents/Library/LaunchAgents/com.picmao.agent-runtime-runtime-service.plist"
            service = plistlib.loads(bundled_plist.read_bytes())
            self.assertEqual(service["BundleProgram"], "Contents/MacOS/AgentRuntimeRuntimeService")
            service_state = home / "Library/Application Support/Agent Runtime/test-service-state.json"
            self.assertEqual(
                json.loads(service_state.read_text()),
                {"main_app": "enabled", "runtime_agent": "enabled"},
            )
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            self.assertEqual(canonical.stat().st_mode & 0o777, 0o600)
            self.assertEqual(env_file.read_bytes(), source_before)
            canonical_text = canonical.read_text()
            self.assertIn(f"AGENT_RUNTIME_WORKSPACE_ROOT={repo.parent.resolve()}\n", canonical_text)
            self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n", canonical_text)

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
            self.assertTrue(client.is_symlink())
            self.assertFalse((home / "Library/LaunchAgents/com.picmao.agent-runtime-runtime.plist").exists())
            service_state = home / "Library/Application Support/Agent Runtime/test-service-state.json"
            self.assertEqual(
                json.loads(service_state.read_text()),
                {"main_app": "enabled", "runtime_agent": "enabled"},
            )

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

    def test_install_rejects_reentry_while_pending_then_allows_reinstall_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo, home, bin_dir, capture = self._install_fixture(Path(raw))
            env_file = self._env(repo, "bootstrap-id")
            first = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_bytes = env_file.read_bytes()
            self.assertIn(b"CONTROL_PLANE_TUNNEL_ID=bootstrap-id", first_bytes)
            self.assertFalse((home / ".config/tunnel-client/agent-runtime.yaml").exists())

            second = self._run_install(repo, home, bin_dir, capture)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already pending", second.stderr)

            committed = self._run_install(repo, home, bin_dir, capture, args=("--commit-cutover",))
            self.assertEqual(committed.returncode, 0, committed.stderr)
            third = self._run_install(repo, home, bin_dir, capture)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(env_file.read_bytes(), first_bytes)
            self.assertEqual(capture.read_text().count("argv=doctor "), 3)

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
