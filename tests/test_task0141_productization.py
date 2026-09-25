from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    "task0141_install_preflight",
    ROOT / "macos" / "install_preflight.py",
)
assert PREFLIGHT_SPEC is not None and PREFLIGHT_SPEC.loader is not None
preflight = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(preflight)

DOCTOR_REASON_CODES = {
    "WORKSPACE_UNAVAILABLE",
    "WORKSPACE_INVALID",
    "RUNTIME_LIMIT_CONFIG_INVALID",
    "GIT_UNAVAILABLE",
    "GIT_IDENTITY_UNAVAILABLE",
    "APP_NOT_INSTALLED",
    "INSTALLED_PACKAGE_INVALID",
    "SERVICE_APPROVAL_REQUIRED",
    "SERVICE_NOT_REGISTERED",
    "SERVICE_STATUS_INVALID",
    "CUTOVER_TRANSACTION_PRESENT",
    "CUTOVER_STATE_INVALID",
    "RUNTIME_IDENTITY_MISMATCH",
    "CAPABILITY_REGISTRY_MISMATCH",
    "SCHEMA_EXPORT_INVALID",
    "TOOL_CONTRACT_SCHEMA_MISMATCH",
    "GOVERNANCE_PROTECTION_INVALID",
    "INTERNAL_SERIALIZATION_FAILURE",
}
ACTION_CLASSES = {"SAFE_AUTOMATED", "HUMAN_ACTION_REQUIRED", "STOP_AND_ESCALATE"}


class Task0141ProductizationTests(unittest.TestCase):
    def _snapshot(self, *roots: Path) -> tuple[tuple[object, ...], ...]:
        rows: list[tuple[object, ...]] = []
        for root in roots:
            if not root.exists():
                rows.append((str(root), "missing"))
                continue
            for path in sorted(root.rglob("*"), key=lambda value: os.fsencode(str(value.relative_to(root)))):
                rel = str(path.relative_to(root))
                info = path.lstat()
                mode = stat.S_IMODE(info.st_mode)
                if path.is_symlink():
                    rows.append((str(root), rel, "symlink", mode, os.readlink(path)))
                elif path.is_dir():
                    rows.append((str(root), rel, "dir", mode))
                elif path.is_file():
                    rows.append(
                        (
                            str(root),
                            rel,
                            "file",
                            mode,
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                        )
                    )
        return tuple(rows)

    def _ready_preflight_fixture(self, temp: Path):
        workspace = temp / "workspace"
        root = workspace / "agent-runtime"
        home = temp / "home"
        tools = temp / "tools"
        root.mkdir(parents=True)
        home.mkdir()
        tools.mkdir()
        (root / "macos").mkdir()
        (root / "macos" / "packaging_python.sh").write_text("# fixture\n")
        tunnel = tools / "tunnel-client"
        tunnel.write_text("#!/bin/sh\nexit 0\n")
        tunnel.chmod(0o700)
        tunnel_id = "task0141-fixture-tunnel"
        api_key = "TASK0141_SECRET_API_KEY"
        git_name = "TASK0141_SECRET_GIT_NAME"
        git_email = "task0141-secret@example.invalid"
        (root / ".env").write_text(
            f"CONTROL_PLANE_API_KEY={api_key}\n"
            f"CONTROL_PLANE_TUNNEL_ID={tunnel_id}\n"
            "AGENT_RUNTIME_WORKSPACE_ROOT=\n"
            f"AGENT_RUNTIME_GIT_NAME={git_name}\n"
            f"AGENT_RUNTIME_GIT_EMAIL={git_email}\n"
            "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n"
            "AGENT_RUNTIME_MAX_PARALLELISM=6\n"
        )
        environ = {
            "PATH": str(tools),
            "HOME": str(home),
            "AGENT_RUNTIME_CODESIGN_IDENTITY": "fixture-signing-identity",
        }

        def fake_run(argv: list[str], *, env=None):
            if argv[:2] == ["/usr/bin/git", "-C"] and argv[3:] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(argv, 0, str(Path(argv[2]).resolve()) + "\n", "")
            if argv[:2] == ["/usr/bin/git", "-C"] and argv[3:] == ["config", "--get", "remote.origin.url"]:
                return subprocess.CompletedProcess(
                    argv, 0, "https://github.com/phatnguyen03022001/agent-runtime.git\n", ""
                )
            if argv == ["/usr/bin/xcrun", "--find", "swift"]:
                return subprocess.CompletedProcess(argv, 0, "/usr/bin/swift\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "")

        def fake_which(name: str, path=None):
            return str(tunnel)

        patches = (
            mock.patch.object(preflight.platform, "system", return_value="Darwin"),
            mock.patch.object(preflight.platform, "machine", return_value="arm64"),
            mock.patch.object(preflight, "_packaging_python_check", return_value=(True, "ready")),
            mock.patch.object(preflight, "_run", side_effect=fake_run),
            mock.patch.object(preflight.shutil, "which", side_effect=fake_which),
            mock.patch.object(
                preflight,
                "EXPECTED_TUNNEL_FINGERPRINT",
                hashlib.sha256(tunnel_id.encode()).hexdigest()[:12],
            ),
        )
        secrets = (api_key, tunnel_id, git_name, git_email)
        return root, home, environ, patches, secrets

    def test_preflight_is_deterministic_read_only_and_secret_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, home, environ, patches, secrets = self._ready_preflight_fixture(Path(td))
            before = self._snapshot(root, home)
            entered = [patcher.start() for patcher in patches]
            try:
                first = preflight.collect_report(root, home, environ=environ)
                second = preflight.collect_report(root, home, environ=environ)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
                del entered
            after = self._snapshot(root, home)
            self.assertEqual(before, after)
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first, second)
            encoded = json.dumps(first, sort_keys=True)
            human = preflight.render_human(first)
            for secret in secrets:
                self.assertNotIn(secret, encoded)
                self.assertNotIn(secret, human)
            self.assertTrue(all(check["status"] == "pass" for check in first["checks"]))

    def test_preflight_nonpass_has_reason_and_action_class(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, home, environ, patches, _ = self._ready_preflight_fixture(Path(td))
            environ.pop("AGENT_RUNTIME_CODESIGN_IDENTITY")
            for patcher in patches:
                patcher.start()
            try:
                report = preflight.collect_report(root, home, environ=environ)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
            self.assertEqual(report["status"], "blocked")
            failures = [item for item in report["checks"] if item["status"] != "pass"]
            self.assertTrue(failures)
            for item in failures:
                self.assertRegex(item["reason_code"], r"^[A-Z0-9_]+$")
                self.assertIn(item["action_class"], ACTION_CLASSES)
            signing = next(item for item in failures if item["id"] == "signing_prerequisite")
            self.assertEqual(signing["reason_code"], "SIGNING_IDENTITY_REQUIRED")
            self.assertEqual(signing["action_class"], "HUMAN_ACTION_REQUIRED")

    def test_install_check_help_and_dispatch_are_read_only_surfaces(self) -> None:
        installer = (ROOT / "install.sh").read_text()
        self.assertIn("./install.sh --check [--json]", installer)
        self.assertIn('exec /usr/bin/python3 "$ROOT/macos/install_preflight.py"', installer)
        help_result = subprocess.run(
            [str(ROOT / "install.sh"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--check [--json]", help_result.stdout)
        self.assertIn("strict read-only", help_result.stdout)

    def test_installed_doctor_uses_package_bytes_and_redacts_transport_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            home = temp / "home"
            app = home / "Applications" / "Agent Runtime.app"
            runtime = app / "Contents" / "Resources" / "runtime"
            (runtime / ".venv" / "bin").mkdir(parents=True)
            (runtime / "agent_runtime").mkdir()
            (runtime / "macos").mkdir()
            shutil.copy2(ROOT / "start.sh", runtime / "start.sh")
            (runtime / "start.sh").chmod(0o700)
            shutil.copy2(ROOT / "macos" / "runtime_config.py", runtime / "macos" / "runtime_config.py")
            (runtime / "agent_runtime" / "doctor.py").write_text("# fixture installed doctor\n")
            fake_python = runtime / ".venv" / "bin" / "python"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '{\"installed\":true,\"bytecode\":\"%s\","
                "\"api_present\":\"%s\",\"tunnel_present\":\"%s\"}\\n' "
                "\"$PYTHONDONTWRITEBYTECODE\" "
                "\"${CONTROL_PLANE_API_KEY+x}\" "
                "\"${CONTROL_PLANE_TUNNEL_ID+x}\"\n"
            )
            fake_python.chmod(0o700)
            workspace = temp / "workspace"
            workspace.mkdir()
            config = home / "Library" / "Application Support" / "Agent Runtime" / "runtime.env"
            config.parent.mkdir(parents=True)
            config.write_text(
                "CONTROL_PLANE_API_KEY=TASK0141_DOCTOR_SECRET\n"
                "CONTROL_PLANE_TUNNEL_ID=TASK0141_DOCTOR_TUNNEL\n"
                f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                "AGENT_RUNTIME_GIT_NAME=Doctor Fixture\n"
                "AGENT_RUNTIME_GIT_EMAIL=doctor-fixture@example.invalid\n"
                "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n"
                "AGENT_RUNTIME_MAX_PARALLELISM=6\n"
            )
            config.chmod(0o600)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env.pop("PYTHONPATH", None)
            env.pop("AGENT_RUNTIME_REVISION", None)
            result = subprocess.run(
                [str(runtime / "start.sh"), "doctor", "--json"],
                cwd=temp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIs(payload["installed"], True)
            self.assertEqual(payload["bytecode"], "1")
            self.assertEqual(payload["api_present"], "")
            self.assertEqual(payload["tunnel_present"], "")
            self.assertNotIn("TASK0141_DOCTOR_SECRET", result.stdout + result.stderr)
            self.assertNotIn("TASK0141_DOCTOR_TUNNEL", result.stdout + result.stderr)

    def test_missing_installed_app_has_deterministic_doctor_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["HOME"] = str(Path(td) / "home")
            result = subprocess.run(
                [str(ROOT / "start.sh"), "doctor", "--json"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["reason_code"], "APP_NOT_INSTALLED")

    def test_recovery_matrix_covers_each_doctor_reason_once(self) -> None:
        recovery = (ROOT / "docs" / "RECOVERY.md").read_text()
        rows = re.findall(
            r"^\| `([A-Z0-9_]+)` \| (SAFE_AUTOMATED|HUMAN_ACTION_REQUIRED|STOP_AND_ESCALATE) \|",
            recovery,
            flags=re.MULTILINE,
        )
        codes = [code for code, _ in rows]
        self.assertEqual(set(codes), DOCTOR_REASON_CODES)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(action in ACTION_CLASSES for _, action in rows))
        mapped = dict(rows)
        self.assertEqual(mapped["SERVICE_APPROVAL_REQUIRED"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(mapped["CUTOVER_STATE_INVALID"], "STOP_AND_ESCALATE")
        self.assertEqual(mapped["INSTALLED_PACKAGE_INVALID"], "STOP_AND_ESCALATE")
        self.assertEqual(mapped["GIT_IDENTITY_UNAVAILABLE"], "HUMAN_ACTION_REQUIRED")

    def test_product_docs_are_current_and_history_independent(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "THREAT_MODEL.md",
            ROOT / ".env.example",
            ROOT / "docs" / "INSTALL.md",
            ROOT / "docs" / "OPERATIONS.md",
            ROOT / "docs" / "RECOVERY.md",
            ROOT / "docs" / "AGENT.md",
        ]
        combined = "\n".join(path.read_text() for path in paths)
        self.assertIsNone(re.search(r"TASK-\d+", combined))
        self.assertNotIn("cloudflare", combined.lower())
        self.assertIn("OpenAI Secure MCP Tunnel", combined)
        self.assertIn("./install.sh --check --json", combined)
        self.assertIn("./start.sh doctor --json", combined)
        self.assertIn("HUMAN_ACTION_REQUIRED", combined)
        self.assertIn("STOP_AND_ESCALATE", combined)
        self.assertIn("VISUAL_PERCEPTION_BLOCKED", combined)

    def test_cli_and_documentation_agree_on_public_operator_commands(self) -> None:
        readme = (ROOT / "README.md").read_text()
        operations = (ROOT / "docs" / "OPERATIONS.md").read_text()
        agent = (ROOT / "docs" / "AGENT.md").read_text()
        installer = (ROOT / "install.sh").read_text()
        starter = (ROOT / "start.sh").read_text()
        for command in (
            "./start.sh start",
            "./start.sh stop",
            "./start.sh restart",
            "./start.sh status",
            "./start.sh session-limit",
            "./start.sh doctor",
        ):
            self.assertIn(command, readme + operations + agent)
        self.assertIn("--check [--json]", installer)
        self.assertIn("doctor [--json]", starter)
        self.assertNotIn("./start.sh --serve", readme + operations + agent)


if __name__ == "__main__":
    unittest.main()
