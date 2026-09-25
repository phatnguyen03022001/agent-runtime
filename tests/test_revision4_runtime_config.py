from __future__ import annotations

import hashlib
import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SPEC = importlib.util.spec_from_file_location(
    "runtime_config_revision4",
    ROOT / "macos" / "runtime_config.py",
)
assert CONFIG_SPEC is not None and CONFIG_SPEC.loader is not None
runtime_config = importlib.util.module_from_spec(CONFIG_SPEC)
CONFIG_SPEC.loader.exec_module(runtime_config)

PROVENANCE_SPEC = importlib.util.spec_from_file_location(
    "package_provenance_task0151",
    ROOT / "macos" / "package_provenance.py",
)
assert PROVENANCE_SPEC is not None and PROVENANCE_SPEC.loader is not None
package_provenance = importlib.util.module_from_spec(PROVENANCE_SPEC)
PROVENANCE_SPEC.loader.exec_module(package_provenance)


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
            "AGENT_RUNTIME_GIT_NAME=Runtime Fixture\n"
            "AGENT_RUNTIME_GIT_EMAIL=runtime-fixture@example.invalid\n"
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
            "XDG_CONFIG_HOME AGENT_RUNTIME_TUNNEL_PROFILE AGENT_RUNTIME_GIT_NAME "
            "AGENT_RUNTIME_GIT_EMAIL; do "
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
                "AGENT_RUNTIME_GIT_NAME": "ambient-wrong-name",
                "AGENT_RUNTIME_GIT_EMAIL": "ambient-wrong@example.invalid",
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
            self.assertEqual(sum(line == "env:AGENT_RUNTIME_GIT_NAME=Runtime Fixture" for line in lines), 2)
            self.assertEqual(
                sum(line == "env:AGENT_RUNTIME_GIT_EMAIL=runtime-fixture@example.invalid" for line in lines),
                2,
            )
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

    def test_installed_serve_binds_revision_from_validated_package_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            home = (temp / "home").resolve()
            runtime = home / "Applications/Agent Runtime.app/Contents/Resources/runtime"
            (runtime / "agent_runtime").mkdir(parents=True)
            (runtime / "macos").mkdir()
            (runtime / ".venv/bin").mkdir(parents=True)
            shutil.copy2(ROOT / "start.sh", runtime / "start.sh")
            (runtime / "start.sh").chmod(0o700)
            shutil.copy2(ROOT / "macos/runtime_config.py", runtime / "macos/runtime_config.py")
            shutil.copy2(ROOT / "macos/package_provenance.py", runtime / "macos/package_provenance.py")
            (runtime / "agent_runtime/server.py").write_text("# fixture runtime payload\n")
            fake_python = runtime / ".venv/bin/python"
            fake_python.write_text("#!/bin/sh\nexit 0\n")
            fake_python.chmod(0o700)

            workspace = temp / "workspace"
            workspace.mkdir()
            config = home / "Library/Application Support/Agent Runtime/runtime.env"
            config.parent.mkdir(parents=True)
            config.write_text(
                "CONTROL_PLANE_API_KEY=test-key\n"
                "CONTROL_PLANE_TUNNEL_ID=stable-test-id\n"
                f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                "AGENT_RUNTIME_GIT_NAME=Runtime Fixture\n"
                "AGENT_RUNTIME_GIT_EMAIL=runtime-fixture@example.invalid\n"
                f"AGENT_RUNTIME_REVISION={'e' * 40}\n"
            )
            config.chmod(0o600)

            tools = temp / "tools"
            tools.mkdir()
            capture = temp / "capture.log"
            tunnel = tools / "tunnel-client"
            tunnel.write_text(
                "#!/bin/bash\n"
                f"printf 'revision=%s\\n' \"${{AGENT_RUNTIME_REVISION-}}\" >> {str(capture)!r}\n"
                "exit 0\n"
            )
            tunnel.chmod(0o700)

            revision = "a" * 40
            tree = "b" * 40
            package_provenance.write_manifest(
                runtime,
                runtime.parent / "runtime-manifest.json",
                revision,
                tree,
                "c" * 64,
            )

            env = {
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "AGENT_RUNTIME_REVISION": "d" * 40,
            }
            result = subprocess.run(
                [str(runtime / "start.sh"), "--serve", str(tunnel), str(config)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text().splitlines(), [f"revision={revision}", f"revision={revision}"])

            capture.unlink()
            (runtime / "agent_runtime/server.py").write_text("# tampered fixture runtime payload\n")
            rejected = subprocess.run(
                [str(runtime / "start.sh"), "--serve", str(tunnel), str(config)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(capture.exists())
            self.assertIn("Installed Runtime package provenance is invalid", rejected.stderr)

    def test_packaged_doctor_import_is_checkout_free(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            runtime = temp / "runtime"
            (runtime / "agent_runtime").mkdir(parents=True)
            (runtime / "macos").mkdir()
            for source in (ROOT / "agent_runtime").glob("*.py"):
                shutil.copy2(source, runtime / "agent_runtime" / source.name)
            for name in ("runtime_config.py", "package_provenance.py", "candidate_cutover.py"):
                shutil.copy2(ROOT / "macos" / name, runtime / "macos" / name)

            env = {
                "HOME": str(temp / "home"),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONPATH": str(runtime),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                [sys.executable, "-c", "import agent_runtime.doctor"],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

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
        service_owner = home / "Applications/Agent Runtime.app/Contents/MacOS/AgentRuntimeMenuBar"
        service_owner.parent.mkdir(parents=True)
        service_owner.write_text(
            "#!/bin/sh\n"
            "if [ \"${1-}\" = \"--service-management\" ] && [ \"${2-}\" = \"status\" ]; then\n"
            "  printf '%s\n' '{\"main_app\":\"enabled\",\"runtime_agent\":\"enabled\"}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n"
        )
        service_owner.chmod(0o700)
        installed_app = home / "Applications/Agent Runtime.app"
        helper = installed_app / "Contents/MacOS/AgentRuntimeRuntimeService"
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o700)
        service_plist = installed_app / "Contents/Library/LaunchAgents/com.picmao.agent-runtime-runtime-service.plist"
        service_plist.parent.mkdir(parents=True)
        service_plist.write_bytes(plistlib.dumps({
            "Label": "com.picmao.agent-runtime-runtime-service",
            "BundleProgram": "Contents/MacOS/AgentRuntimeRuntimeService",
        }))
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

    def test_runtime_config_migrates_missing_git_identity_atomically_and_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            repo = temp / "repo"
            workspace = temp / "workspace"
            repo.mkdir()
            workspace.mkdir()
            source = repo / ".env"
            source.write_text(
                "CONTROL_PLANE_API_KEY=test-key\n"
                "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
            )
            source.chmod(0o600)
            canonical = temp / "config" / "runtime.env"
            canonical.parent.mkdir()
            canonical.write_bytes(source.read_bytes())
            canonical.chmod(0o600)
            before = canonical.read_bytes()
            real_replace = runtime_config.os.replace
            replaced = False

            def atomic_replace(source_path, destination_path) -> None:
                nonlocal replaced
                private = Path(source_path)
                self.assertEqual(canonical.read_bytes(), before)
                self.assertEqual(private.stat().st_mode & 0o777, 0o600)
                replaced = True
                real_replace(source_path, destination_path)

            with mock.patch.dict(
                runtime_config.os.environ,
                {
                    "AGENT_RUNTIME_GIT_NAME": "Installer Fixture",
                    "AGENT_RUNTIME_GIT_EMAIL": "installer-fixture@example.invalid",
                },
                clear=False,
            ), mock.patch.object(runtime_config.os, "replace", side_effect=atomic_replace):
                runtime_config.ensure(source, canonical, workspace, require_git_identity=True)

            self.assertTrue(replaced)
            migrated = canonical.read_bytes()
            self.assertTrue(migrated.startswith(before))
            self.assertIn(b"AGENT_RUNTIME_GIT_NAME=Installer Fixture\n", migrated)
            self.assertIn(b"AGENT_RUNTIME_GIT_EMAIL=installer-fixture@example.invalid\n", migrated)
            self.assertEqual(canonical.stat().st_mode & 0o777, 0o600)

            with mock.patch.dict(
                runtime_config.os.environ,
                {
                    "AGENT_RUNTIME_GIT_NAME": "Replacement Must Not Win",
                    "AGENT_RUNTIME_GIT_EMAIL": "replacement@example.invalid",
                },
                clear=False,
            ):
                runtime_config.ensure(source, canonical, workspace, require_git_identity=True)
            self.assertEqual(canonical.read_bytes(), migrated)

    def test_runtime_config_git_identity_fallback_and_validation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            repo = temp / "repo"
            workspace = temp / "workspace"
            repo.mkdir()
            workspace.mkdir()
            subprocess.run(["/usr/bin/git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(
                ["/usr/bin/git", "-C", str(repo), "config", "--local", "user.name", "Local Fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "user.email",
                    "local-fixture@example.invalid",
                ],
                check=True,
            )
            source = repo / ".env"
            source.write_text(
                "CONTROL_PLANE_API_KEY=test-key\n"
                "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
            )
            source.chmod(0o600)
            canonical = temp / "config" / "runtime.env"

            with mock.patch.dict(
                runtime_config.os.environ,
                {"AGENT_RUNTIME_GIT_NAME": "", "AGENT_RUNTIME_GIT_EMAIL": ""},
                clear=False,
            ):
                runtime_config.ensure(source, canonical, workspace, require_git_identity=True)
            text = canonical.read_text()
            self.assertIn("AGENT_RUNTIME_GIT_NAME=Local Fixture\n", text)
            self.assertIn("AGENT_RUNTIME_GIT_EMAIL=local-fixture@example.invalid\n", text)

            for name, email in (
                ("Partial Fixture", ""),
                ("x" * 257, "valid@example.invalid"),
                ("bad\nname", "valid@example.invalid"),
            ):
                with self.subTest(name_length=len(name), email_present=bool(email)):
                    candidate = temp / f"candidate-{len(name)}-{bool(email)}.env"
                    with mock.patch.dict(
                        runtime_config.os.environ,
                        {"AGENT_RUNTIME_GIT_NAME": name, "AGENT_RUNTIME_GIT_EMAIL": email},
                        clear=False,
                    ):
                        with self.assertRaises(SystemExit) as caught:
                            runtime_config.ensure(
                                source,
                                candidate,
                                workspace,
                                require_git_identity=True,
                            )
                    self.assertFalse(candidate.exists())
                    self.assertNotIn(name, str(caught.exception))

            duplicate = temp / "duplicate.env"
            duplicate.write_text(
                "CONTROL_PLANE_API_KEY=test-key\n"
                "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                "AGENT_RUNTIME_GIT_NAME=One\n"
                "AGENT_RUNTIME_GIT_NAME=Two\n"
                "AGENT_RUNTIME_GIT_EMAIL=one@example.invalid\n"
            )
            duplicate.chmod(0o600)
            with self.assertRaisesRegex(SystemExit, "duplicate AGENT_RUNTIME_GIT_NAME"):
                runtime_config.ensure(source, duplicate, workspace, require_git_identity=True)


if __name__ == "__main__":
    unittest.main()
