from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL = PROJECT_ROOT / "install.sh"
CANONICAL_REMOTE = "https://github.com/phatnguyen03022001/agent-runtime.git"
TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"
API_KEY = "sk-test-runtime-secret"


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(INSTALL.exists(), "install.sh must exist before installer behavior can pass")

        self.temp = tempfile.TemporaryDirectory(prefix="agent-runtime-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.fakebin = self.root / "fakebin"
        self.home.mkdir()
        self.fakebin.mkdir()

        git = shutil.which("git")
        self.assertIsNotNone(git)
        python = Path(sys.executable)
        (self.fakebin / "git").symlink_to(git)
        (self.fakebin / "python3").symlink_to(python)

        _run([git, "init", "--bare", str(self.remote)], cwd=self.root)
        clone = _run([git, "clone", str(self.remote), str(self.repo)], cwd=self.root)
        self.assertEqual(clone.returncode, 0, clone.stdout)
        branch = _run([git, "-C", str(self.repo), "switch", "-c", "dev"], cwd=self.root)
        self.assertEqual(branch.returncode, 0, branch.stdout)

        (self.repo / ".gitignore").write_text(".env\n.venv/\n")
        (self.repo / ".env.example").write_text(
            "AGENT_RUNTIME_CONFIG=/absolute/path/to/runtime.local.toml\n"
            "AGENT_RUNTIME_TUNNEL_PROFILE=agent-runtime\n"
            "CONTROL_PLANE_API_KEY=\n"
            "CONTROL_PLANE_TUNNEL_ID=\n"
        )
        (self.repo / "requirements.txt").write_text("mcp==2.0.0\n")
        _write_executable(self.repo / "verify", "#!/usr/bin/env bash\nset -euo pipefail\necho VERIFY_PASS\n")
        shutil.copy2(INSTALL, self.repo / "install.sh")
        (self.repo / "install.sh").chmod((self.repo / "install.sh").stat().st_mode | stat.S_IXUSR)

        commit = _run(
            [
                git,
                "-C",
                str(self.repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "add",
                ".",
            ],
            cwd=self.root,
        )
        self.assertEqual(commit.returncode, 0, commit.stdout)
        commit = _run(
            [
                git,
                "-C",
                str(self.repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=self.root,
        )
        self.assertEqual(commit.returncode, 0, commit.stdout)
        push = _run([git, "-C", str(self.repo), "push", "-u", "origin", "dev"], cwd=self.root)
        self.assertEqual(push.returncode, 0, push.stdout)

        gitconfig = self.home / ".gitconfig"
        config = _run(
            [git, "config", "--file", str(gitconfig), f"url.{self.remote}.insteadOf", CANONICAL_REMOTE],
            cwd=self.root,
        )
        self.assertEqual(config.returncode, 0, config.stdout)
        set_url = _run([git, "-C", str(self.repo), "remote", "set-url", "origin", CANONICAL_REMOTE], cwd=self.root)
        self.assertEqual(set_url.returncode, 0, set_url.stdout)

        venv_bin = self.repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        _write_executable(
            venv_bin / "python",
            "#!/usr/bin/env bash\nset -euo pipefail\n# Tests only need pip/setup commands to succeed.\nexit 0\n",
        )

        self.brew_log = self.root / "brew.log"
        self.tunnel_log = self.root / "tunnel.log"
        self.tunnel_template = self.root / "tunnel-client-template"
        _write_executable(
            self.tunnel_template,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >> \"$TUNNEL_LOG\"\n"
            "case \"${1:-}\" in\n"
            "  --version) echo 'tunnel-client test'; exit 0 ;;\n"
            "  init)\n"
            "    if [[ \"${2:-}\" == '--help' ]]; then exit 0; fi\n"
            "    mkdir -p \"$HOME/.config/tunnel-client\"\n"
            "    printf 'generated-profile\\n' > \"$HOME/.config/tunnel-client/agent-runtime.yaml\"\n"
            "    exit 0 ;;\n"
            "  doctor) exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
        )
        _write_executable(
            self.fakebin / "brew",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >> \"$BREW_LOG\"\n"
            "if [[ \"$*\" == 'install openai/tools/tunnel-client' ]]; then\n"
            "  cp \"$TUNNEL_TEMPLATE\" \"$FAKE_BIN/tunnel-client\"\n"
            "  chmod +x \"$FAKE_BIN/tunnel-client\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 64\n",
        )
        _write_executable(self.fakebin / "uname", "#!/usr/bin/env bash\necho Darwin\n")

        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fakebin}:/usr/bin:/bin",
                "BREW_LOG": str(self.brew_log),
                "TUNNEL_LOG": str(self.tunnel_log),
                "TUNNEL_TEMPLATE": str(self.tunnel_template),
                "FAKE_BIN": str(self.fakebin),
            }
        )

    def _install_tunnel_client(self) -> None:
        shutil.copy2(self.tunnel_template, self.fakebin / "tunnel-client")
        (self.fakebin / "tunnel-client").chmod((self.fakebin / "tunnel-client").stat().st_mode | stat.S_IXUSR)

    def _write_env(self, *, with_credentials: bool) -> None:
        api_key = API_KEY if with_credentials else ""
        tunnel_id = TUNNEL_ID if with_credentials else ""
        (self.repo / ".env").write_text(
            "AGENT_RUNTIME_CONFIG=/absolute/path/to/runtime.local.toml\n"
            "AGENT_RUNTIME_TUNNEL_PROFILE=agent-runtime\n"
            f"CONTROL_PLANE_API_KEY={api_key}\n"
            f"CONTROL_PLANE_TUNNEL_ID={tunnel_id}\n"
        )

    def _run_install(self) -> subprocess.CompletedProcess[str]:
        return _run(["bash", "./install.sh"], cwd=self.repo, env=self.env)

    def test_missing_tunnel_client_uses_official_homebrew_and_never_runs_daemon(self) -> None:
        self._write_env(with_credentials=True)

        result = self._run_install()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("install openai/tools/tunnel-client", self.brew_log.read_text())
        tunnel_log = self.tunnel_log.read_text()
        self.assertIn("init ", tunnel_log)
        self.assertIn("doctor ", tunnel_log)
        self.assertNotIn("run ", tunnel_log)
        self.assertNotIn(API_KEY, result.stdout)
        self.assertNotIn(TUNNEL_ID, result.stdout)
        self.assertTrue((self.home / ".config/agent-runtime/runtime.local.toml").is_file())
        self.assertTrue((self.home / ".config/tunnel-client/agent-runtime.yaml").is_file())

    def test_existing_profile_is_preserved_and_rerun_is_idempotent(self) -> None:
        self._install_tunnel_client()
        self._write_env(with_credentials=True)
        profile = self.home / ".config/tunnel-client/agent-runtime.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text("sentinel-profile\n")

        first = self._run_install()
        second = self._run_install()

        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual(profile.read_text(), "sentinel-profile\n")
        tunnel_log = self.tunnel_log.read_text()
        self.assertNotIn("init ", tunnel_log)
        self.assertGreaterEqual(tunnel_log.count("doctor "), 2)
        self.assertNotIn("run ", tunnel_log)
        self.assertFalse(self.brew_log.exists() and self.brew_log.read_text().strip())

    def test_missing_credentials_stops_before_profile_creation_or_doctor(self) -> None:
        self._install_tunnel_client()
        self._write_env(with_credentials=False)

        result = self._run_install()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONTROL_PLANE_API_KEY", result.stdout)
        self.assertIn("CONTROL_PLANE_TUNNEL_ID", result.stdout)
        self.assertFalse((self.home / ".config/tunnel-client/agent-runtime.yaml").exists())
        tunnel_log = self.tunnel_log.read_text() if self.tunnel_log.exists() else ""
        self.assertNotIn("doctor ", tunnel_log)
        self.assertNotIn("run ", tunnel_log)

    def test_unknown_existing_disposable_checkout_is_preserved_and_blocks(self) -> None:
        self._install_tunnel_client()
        self._write_env(with_credentials=True)
        checkout = self.home / ".local/share/agent-runtime/checkouts/agent-runtime-dev"
        checkout.mkdir(parents=True)
        marker = checkout / "DO_NOT_DELETE"
        marker.write_text("user-state\n")

        result = self._run_install()

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(), "user-state\n")
        self.assertIn("existing disposable checkout", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
