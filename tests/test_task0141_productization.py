from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
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
    "INSTALLED_SUBSTRATE_INVALID",
    "CANONICAL_CONFIG_MISSING",
    "LIFECYCLE_OWNERSHIP_INVALID",
    "LIFECYCLE_OWNERSHIP_CONTRADICTORY",
    "MAIN_APP_REGISTRATION_ABSENT",
    "MAIN_APP_APPROVAL_REQUIRED",
    "PAYLOAD_POINTER_MISSING",
    "PAYLOAD_POINTER_INVALID",
    "PAYLOAD_RELEASE_MISSING",
    "PAYLOAD_RELEASE_INVALID",
    "PAYLOAD_SUBSTRATE_INCOMPATIBLE",
    "PAYLOAD_SELECTION_INVALID",
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
            (root / ".env").unlink()
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
            config = next(item for item in failures if item["id"] == "checkout_config")
            self.assertEqual(config["reason_code"], "CHECKOUT_CONFIG_MISSING")
            self.assertEqual(config["action_class"], "HUMAN_ACTION_REQUIRED")

    def test_preflight_current_lifecycle_repairs_missing_main_and_rejects_competing_runtime_sm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, home, environ, patches, _ = self._ready_preflight_fixture(Path(td))
            app = home / "Applications" / "Agent Runtime.app"
            app.mkdir(parents=True)
            current_plist = home / "Library" / "LaunchAgents" / "com.picmao.agent-runtime-runtime-service.plist"
            current_plist.parent.mkdir(parents=True)
            current_plist.write_text("fixture\n")
            helper = app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
            state = {"main_app": "not-found", "runtime_agent": "not-found"}

            extra = (
                mock.patch.object(
                    preflight.candidate_cutover,
                    "_validate_current_launchagent",
                    return_value={"Label": preflight.candidate_cutover.MODERN_RUNTIME_LABEL},
                ),
                mock.patch.object(
                    preflight.candidate_cutover,
                    "_loaded_service_program",
                    return_value=helper,
                ),
                mock.patch.object(
                    preflight.candidate_cutover,
                    "_current_runtime_helper",
                    return_value=helper,
                ),
                mock.patch.object(
                    preflight.candidate_cutover,
                    "_service_management",
                    side_effect=lambda _app, operation: dict(state) if operation == "status" else None,
                ),
            )
            entered = [patcher.start() for patcher in (*patches, *extra)]
            try:
                repairable = preflight.collect_report(root, home, environ=environ)
                lifecycle = next(item for item in repairable["checks"] if item["id"] == "lifecycle_ownership")
                self.assertEqual(repairable["status"], "ready")
                self.assertEqual(lifecycle["status"], "pass")
                self.assertEqual(lifecycle["evidence"]["contract"], "user-launchagent-v1")
                self.assertEqual(lifecycle["evidence"]["main_app"], "not-found")
                self.assertEqual(lifecycle["evidence"]["runtime_agent"], "not-found")

                state["main_app"] = "enabled"
                state["runtime_agent"] = "enabled"
                contradictory = preflight.collect_report(root, home, environ=environ)
                failed = next(item for item in contradictory["checks"] if item["id"] == "lifecycle_ownership")
                self.assertEqual(contradictory["status"], "blocked")
                self.assertEqual(failed["reason_code"], "LIFECYCLE_OWNERSHIP_INVALID")
            finally:
                for patcher in reversed((*patches, *extra)):
                    patcher.stop()
                del entered

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

    def test_installed_doctor_rejects_unqualified_substrate_without_leaking_secrets(self) -> None:
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
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["reason_code"], "PAYLOAD_SELECTION_INVALID")
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
        self.assertEqual(mapped["LIFECYCLE_OWNERSHIP_INVALID"], "STOP_AND_ESCALATE")
        self.assertEqual(mapped["CUTOVER_STATE_INVALID"], "STOP_AND_ESCALATE")
        self.assertEqual(mapped["INSTALLED_SUBSTRATE_INVALID"], "STOP_AND_ESCALATE")
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
        readme = (ROOT / "README.md").read_text()
        recovery = (ROOT / "docs" / "RECOVERY.md").read_text()
        self.assertIsNone(re.search(r"TASK-\d+", combined))
        self.assertNotIn("cloudflare", combined.lower())
        self.assertIn("OpenAI Secure MCP Tunnel", combined)
        self.assertIn("./install.sh --check --json", combined)
        self.assertIn("./start.sh doctor --json", combined)
        self.assertIn("HUMAN_ACTION_REQUIRED", combined)
        self.assertIn("STOP_AND_ESCALATE", combined)
        self.assertIn("VISUAL_PERCEPTION_BLOCKED", combined)
        self.assertIn("installed Runtime is version **0.5.1**", readme)
        self.assertIn("Runtime source and installed Runtime version 0.5.1", readme)
        self.assertIn("installed Runtime 0.5.1", recovery)
        self.assertNotIn("installed Runtime is version **0.5.0**", readme)
        self.assertNotIn("Runtime source and installed Runtime version 0.5.0", readme)
        self.assertNotIn("installed Runtime 0.5.0", recovery)

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

    def test_prebuilt_preflight_is_checkout_independent_and_secret_safe(self) -> None:
        self.assertTrue(hasattr(preflight, "collect_prebuilt_report"), "prebuilt preflight must exist")
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            bundle = temp / "release"
            home = temp / "home"
            workspace = temp / "workspace"
            tools = temp / "tools"
            app = bundle / "Agent Runtime.app"
            runtime = app / "Contents" / "Resources" / "runtime"
            embedded_python = runtime / ".venv" / "bin" / "python"
            embedded_macos = runtime / "macos"
            handoff = bundle / "Agent Runtime.candidate.json"
            for directory in (home, workspace, tools, embedded_python.parent, embedded_macos):
                directory.mkdir(parents=True, exist_ok=True)
            embedded_python.write_text("#!/bin/sh\nexit 0\n")
            embedded_python.chmod(0o700)
            for name in ("runtime_config.py", "package_provenance.py", "candidate_cutover.py", "install_preflight.py"):
                (embedded_macos / name).write_text("# fixture\n")
            closure = "a" * 64
            handoff.write_text(json.dumps({"initial_payload_closure": closure}) + "\n")
            (bundle / "payloads" / closure).mkdir(parents=True)
            tunnel_target = tools / "tunnel-client-real"
            tunnel_target.write_text("#!/bin/sh\nexit 0\n")
            tunnel_target.chmod(0o700)
            tunnel = tools / "tunnel-client"
            tunnel.symlink_to(tunnel_target)

            tunnel_id = "task0171-prebuilt-tunnel"
            secrets = (
                "TASK0171_PREBUILT_API_SECRET",
                tunnel_id,
                "Task 0171 Operator",
                "task0171@example.invalid",
            )
            environ = {
                "PATH": str(tools),
                "CONTROL_PLANE_API_KEY": secrets[0],
                "CONTROL_PLANE_TUNNEL_ID": secrets[1],
                "AGENT_RUNTIME_GIT_NAME": secrets[2],
                "AGENT_RUNTIME_GIT_EMAIL": secrets[3],
            }

            def fake_which(name: str, path=None):
                if name == "tunnel-client":
                    return str(tunnel)
                if name in {"launchctl", "lsof", "curl"}:
                    return "/usr/bin/true"
                return None

            with mock.patch.object(preflight.platform, "system", return_value="Darwin"), mock.patch.object(
                preflight.platform, "machine", return_value="arm64"
            ), mock.patch.object(preflight.shutil, "which", side_effect=fake_which), mock.patch.object(
                preflight, "EXPECTED_TUNNEL_FINGERPRINT", hashlib.sha256(tunnel_id.encode()).hexdigest()[:12]
            ), mock.patch.object(
                preflight.package_provenance, "_load_zero_cost_candidate_handoff", return_value={"initial_payload_closure": closure}
            ), mock.patch.object(
                preflight.package_provenance, "validate_zero_cost_candidate", return_value={"candidate_sha256": "a" * 64}
            ), mock.patch.object(
                preflight, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")
            ):
                report = preflight.collect_prebuilt_report(
                    bundle,
                    workspace,
                    home,
                    environ=environ,
                )

            self.assertEqual(report["status"], "ready")
            ids = {item["id"] for item in report["checks"]}
            self.assertIn("lifecycle_ownership", ids)
            lifecycle = next(item for item in report["checks"] if item["id"] == "lifecycle_ownership")
            self.assertEqual(lifecycle["status"], "pass")
            self.assertFalse(lifecycle["evidence"]["app_present"])
            self.assertNotIn("repository", ids)
            self.assertNotIn("packaging_python", ids)
            self.assertNotIn("developer_toolchain", ids)
            self.assertNotIn("signing_prerequisite", ids)
            encoded = json.dumps(report, sort_keys=True)
            human = preflight.render_human(report)
            for secret in secrets:
                self.assertNotIn(secret, encoded)
                self.assertNotIn(secret, human)

            installed = home / "Applications" / "Agent Runtime.app"
            installed.mkdir(parents=True)
            current_plist = home / "Library" / "LaunchAgents" / "com.picmao.agent-runtime-runtime-service.plist"
            current_plist.parent.mkdir(parents=True)
            current_plist.write_text("fixture\n")
            helper = installed / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
            service_state = {"main_app": "enabled", "runtime_agent": "enabled"}
            with mock.patch.object(preflight.platform, "system", return_value="Darwin"), mock.patch.object(
                preflight.platform, "machine", return_value="arm64"
            ), mock.patch.object(preflight.shutil, "which", side_effect=fake_which), mock.patch.object(
                preflight, "EXPECTED_TUNNEL_FINGERPRINT", hashlib.sha256(tunnel_id.encode()).hexdigest()[:12]
            ), mock.patch.object(
                preflight.package_provenance, "_load_zero_cost_candidate_handoff", return_value={"initial_payload_closure": closure}
            ), mock.patch.object(
                preflight.package_provenance, "validate_zero_cost_candidate", return_value={"candidate_sha256": "a" * 64}
            ), mock.patch.object(
                preflight, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")
            ), mock.patch.object(
                preflight.candidate_cutover, "_validate_current_launchagent", return_value={}
            ), mock.patch.object(
                preflight.candidate_cutover, "_loaded_service_program", return_value=helper
            ), mock.patch.object(
                preflight.candidate_cutover, "_current_runtime_helper", return_value=helper
            ), mock.patch.object(
                preflight.candidate_cutover,
                "_service_management",
                return_value=service_state,
            ):
                contradictory = preflight.collect_prebuilt_report(
                    bundle,
                    workspace,
                    home,
                    environ=environ,
                )
            failed = next(item for item in contradictory["checks"] if item["id"] == "lifecycle_ownership")
            self.assertEqual(contradictory["status"], "blocked")
            self.assertEqual(failed["reason_code"], "LIFECYCLE_OWNERSHIP_INVALID")

    def test_prebuilt_entrypoints_preserve_sealed_app_and_reject_bad_trust(self) -> None:
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        for carrier in ("direct", "installer"):
            with self.subTest(carrier=carrier), tempfile.TemporaryDirectory(
                prefix="task0184-", dir=build
            ) as raw:
                temp = Path(raw)
                bundle = temp / "bundle"
                app = bundle / "Agent Runtime.app"
                runtime = app / "Contents" / "Resources" / "runtime"
                macos = runtime / "macos"
                macos.mkdir(parents=True)
                python = runtime / ".venv" / "bin" / "python"
                python.parent.mkdir(parents=True)
                python.write_text(
                    "#!/bin/sh\n"
                    f"PYTHONPATH={shlex.quote(str(macos))} {shlex.quote(sys.executable)} "
                    "-X pycache_prefix= -c 'import candidate_cutover'\n"
                    f"exec {shlex.quote(sys.executable)} -X pycache_prefix= \"$@\"\n"
                )
                python.chmod(0o700)
                shutil.copy2(ROOT / "macos" / "install_preflight.py", macos / "install_preflight.py")
                shutil.copy2(ROOT / "macos" / "install_release.sh", macos / "install_release.sh")
                (macos / "install_release.sh").chmod(0o700)
                (macos / "candidate_cutover.py").write_text("# fixture module\n")
                (macos / "runtime_config.py").write_text(
                    "def prebuilt_values(*args, **kwargs):\n    raise SystemExit(2)\n"
                )
                (macos / "package_provenance.py").write_text(
                    "class PackageProvenanceError(Exception):\n    pass\n"
                    "def _load_zero_cost_candidate_handoff(path):\n"
                    "    return {'initial_payload_closure': 'a' * 64}\n"
                    "def validate_zero_cost_candidate(*args):\n"
                    "    raise PackageProvenanceError('tampered fixture')\n"
                )
                (bundle / "Agent Runtime.candidate.json").write_text(
                    json.dumps({"initial_payload_closure": "a" * 64}) + "\n"
                )
                (bundle / "payloads" / ("a" * 64)).mkdir(parents=True)
                workspace = temp / "workspace"
                workspace.mkdir()
                home = temp / "home"
                home.mkdir()
                sealed_before = self._snapshot(app)
                env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "LANG": "C"}
                if carrier == "direct":
                    argv = [
                        sys.executable,
                        "-X",
                        "pycache_prefix=",
                        str(macos / "install_preflight.py"),
                        "--prebuilt",
                        "--bundle-root",
                        str(bundle),
                        "--workspace-root",
                        str(workspace),
                        "--json",
                    ]
                else:
                    argv = [
                        "/bin/bash",
                        str(macos / "install_release.sh"),
                        "--workspace-root",
                        str(workspace),
                    ]
                result = subprocess.run(
                    argv, env=env, capture_output=True, text=True, check=False, timeout=20
                )
                self.assertNotEqual(result.returncode, 0)
                if carrier == "direct":
                    report = json.loads(result.stdout)
                    trust = next(item for item in report["checks"] if item["id"] == "release_trust")
                    self.assertEqual(trust["reason_code"], "RELEASE_TRUST_INVALID")
                    self.assertEqual(trust["status"], "fail")
                else:
                    self.assertIn("release_trust: fail (RELEASE_TRUST_INVALID)", result.stdout)
                    self.assertIn("prebuilt installation preflight failed", result.stderr)
                self.assertEqual(self._snapshot(app), sealed_before)


if __name__ == "__main__":
    unittest.main()
