from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_runtime import doctor
from agent_runtime.capability_registry import CAPABILITY_NAMES
from agent_runtime.contracts import DoctorCheck, DoctorReport
from agent_runtime.version import RUNTIME_VERSION

ROOT = Path(__file__).resolve().parents[1]


def valid_env(workspace: Path) -> dict[str, str]:
    return {
        "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
        "AGENT_RUNTIME_MAX_PARALLELISM": "6",
        "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS": "6",
        "AGENT_RUNTIME_GIT_NAME": "Runtime Executor",
        "AGENT_RUNTIME_GIT_EMAIL": "runtime@example.invalid",
    }


def make_report(status: str) -> DoctorReport:
    check_status = {
        "healthy": "pass",
        "degraded": "warn",
        "unhealthy": "fail",
    }[status]
    return DoctorReport(
        schema_version=1,
        runtime_version=RUNTIME_VERSION,
        status=status,
        checks=[
            DoctorCheck(
                id="fixture",
                status=check_status,
                reason_code="FIXTURE",
                message="fixture",
                evidence={},
            )
        ],
    )


def make_installed_fixture(home: Path) -> Path:
    app = home / "Applications" / "Agent Runtime.app"
    resources = app / "Contents" / "Resources"
    runtime = resources / "runtime"
    macos = app / "Contents" / "MacOS"
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    macos.mkdir(parents=True)

    (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (runtime / "start.sh").chmod(0o755)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    for name in (
        "AgentRuntimeMenuBar",
        "AgentRuntimeRuntimeService",
        "AgentRuntimeScreenCapture",
    ):
        executable = macos / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    menu = macos / "AgentRuntimeMenuBar"
    menu.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --service-management ] && [ \"$2\" = status ]; then\n"
        "  printf '%s\\n' '{\"main_app\":\"enabled\",\"runtime_agent\":\"not-found\"}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    menu.chmod(0o755)

    info = {
        "CFBundleIdentifier": doctor.package_provenance.OWNER,
        "CFBundleExecutable": "AgentRuntimeMenuBar",
        "CFBundleShortVersionString": RUNTIME_VERSION,
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))

    surface_blob = ("\n".join(doctor.ADVERTISED_TOOL_NAMES) + "\n").encode("utf-8")
    surface_sha = hashlib.sha256(surface_blob).hexdigest()
    python_major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    lock_sha = "c" * 64
    doctor.package_provenance.write_substrate_manifest(
        runtime,
        resources / "runtime-manifest.json",
        "a" * 40,
        "b" * 40,
        lock_sha,
        python_major_minor=python_major_minor,
        public_tool_count=20,
        public_surface_sha256=surface_sha,
    )

    source_package = home / "payload-source" / "agent_runtime"
    source_package.mkdir(parents=True)
    for source in (ROOT / "agent_runtime").iterdir():
        if source.is_file() and source.suffix == ".py":
            shutil.copy2(source, source_package / source.name)
    state_dir = home / "Library" / "Application Support" / "Agent Runtime"
    published = doctor.package_provenance.publish_payload_release(
        source_package,
        state_dir / "payloads",
        revision="d" * 40,
        tree="e" * 40,
        requirements_lock_sha256=lock_sha,
        python_major_minor=python_major_minor,
        public_tool_count=20,
        public_surface_sha256=surface_sha,
    )
    pointer = state_dir / "current-payload"
    pointer.write_text(str(published["content_closure"]) + "\n", encoding="ascii")
    pointer.chmod(0o600)
    doctor.candidate_cutover._materialize_current_launchagent(home, uid=os.getuid())
    return app


class DoctorContractTests(unittest.TestCase):
    def test_closed_report_and_check_contracts_are_exact(self) -> None:
        self.assertEqual(
            tuple(DoctorReport.model_fields),
            ("schema_version", "runtime_version", "status", "checks"),
        )
        self.assertEqual(
            tuple(DoctorCheck.model_fields),
            ("id", "status", "reason_code", "message", "evidence"),
        )
        with self.assertRaises(Exception):
            DoctorReport(
                schema_version=1,
                runtime_version=RUNTIME_VERSION,
                status="healthy",
                checks=[],
                extra_field=True,
            )

    def test_status_composition_and_exit_codes_are_exact(self) -> None:
        passed = DoctorCheck(id="a", status="pass", reason_code="OK", message="", evidence={})
        warned = DoctorCheck(id="b", status="warn", reason_code="WARN", message="", evidence={})
        failed = DoctorCheck(id="c", status="fail", reason_code="FAIL", message="", evidence={})
        na = DoctorCheck(
            id="d",
            status="not_applicable",
            reason_code="NA",
            message="",
            evidence={},
        )
        self.assertEqual(doctor._compose_status([passed, na]), "healthy")
        self.assertEqual(doctor._compose_status([passed, warned]), "degraded")
        self.assertEqual(doctor._compose_status([passed, failed]), "unhealthy")
        self.assertEqual(doctor.exit_code(make_report("healthy")), 0)
        self.assertEqual(doctor.exit_code(make_report("degraded")), 1)
        self.assertEqual(doctor.exit_code(make_report("unhealthy")), 1)

    def test_registry_schema_and_governance_checks_reuse_current_authorities(self) -> None:
        bundle = doctor._schema_bundle()
        self.assertIsNotNone(bundle)
        self.assertEqual(doctor._runtime_identity_check(bundle).status, "pass")
        registry = doctor._capability_registry_check()
        schema = doctor._tool_contract_schema_check(bundle)
        governance = doctor._governance_protection_check()
        self.assertEqual(registry.status, "pass")
        self.assertEqual(schema.status, "pass")
        self.assertEqual(governance.status, "pass")
        self.assertEqual(len(CAPABILITY_NAMES), 21)
        descriptors = {
            entry["descriptor"]["name"]: entry["descriptor"]
            for entry in bundle["capabilities"]
        }
        self.assertEqual(
            (
                descriptors["fs_read_batch"]["request_schema_version"],
                descriptors["fs_read_batch"]["result_schema_version"],
            ),
            (1, 2),
        )
        self.assertEqual(
            (
                descriptors["fs_manage"]["request_schema_version"],
                descriptors["fs_manage"]["result_schema_version"],
            ),
            (1, 1),
        )
        self.assertEqual(
            (
                descriptors["runtime_capabilities"]["request_schema_version"],
                descriptors["runtime_capabilities"]["result_schema_version"],
            ),
            (2, 3),
        )
        self.assertEqual(registry.evidence["screen_capture"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertEqual(governance.evidence["screen_capture"], "VISUAL_PERCEPTION_BLOCKED")

    def test_schema_check_rejects_task0148_schema_version_regression(self) -> None:
        bundle = doctor._schema_bundle()
        self.assertIsNotNone(bundle)
        for name, field, stale_value in (
            ("fs_read_batch", "result_schema_version", 1),
            ("fs_manage", "request_schema_version", 2),
            ("runtime_capabilities", "result_schema_version", 2),
        ):
            with self.subTest(name=name, field=field):
                stale = json.loads(json.dumps(bundle))
                descriptor = next(
                    entry["descriptor"]
                    for entry in stale["capabilities"]
                    if entry["descriptor"]["name"] == name
                )
                descriptor[field] = stale_value
                check = doctor._tool_contract_schema_check(stale)
                self.assertEqual(check.status, "fail")
                self.assertEqual(check.reason_code, "TOOL_CONTRACT_SCHEMA_MISMATCH")

class DoctorLocalStateTests(unittest.TestCase):
    def test_workspace_and_limit_checks_fail_closed_on_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            self.assertEqual(
                doctor._workspace_check({"AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace)}).status,
                "pass",
            )
            self.assertEqual(
                doctor._workspace_check({"AGENT_RUNTIME_WORKSPACE_ROOT": "relative"}).reason_code,
                "WORKSPACE_INVALID",
            )
            self.assertEqual(
                doctor._capacity_session_config_check(
                    {
                        "AGENT_RUNTIME_MAX_PARALLELISM": "0",
                        "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS": "7",
                    }
                ).status,
                "fail",
            )

    def test_durable_recovery_fault_fails_capacity_readiness_with_stable_reason(self) -> None:
        with patch.object(
            doctor,
            "terminal_recovery_reason",
            return_value="DURABLE_RECOVERY_OVER_CAPACITY",
        ):
            check = doctor._capacity_session_config_check({})
        self.assertEqual(check.status, "fail")
        self.assertEqual(
            check.reason_code,
            "DURABLE_RECOVERY_OVER_CAPACITY",
        )
        self.assertEqual(check.evidence, {"durable_recovery_ready": False})

    def test_git_identity_validation_never_emits_identity_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = valid_env(Path(raw))
            env["AGENT_RUNTIME_GIT_NAME"] = "Secret Person"
            env["AGENT_RUNTIME_GIT_EMAIL"] = "secret-person@example.invalid"
            check = doctor._git_readiness_check(env)
            self.assertEqual(check.status, "pass")
            encoded = json.dumps(check.model_dump(mode="json"))
            self.assertNotIn("Secret Person", encoded)
            self.assertNotIn("secret-person@example.invalid", encoded)

            bad = dict(env)
            bad["AGENT_RUNTIME_GIT_EMAIL"] = "bad\nvalue"
            failed = doctor._git_readiness_check(bad)
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.reason_code, "GIT_IDENTITY_UNAVAILABLE")

    def test_absent_package_and_service_are_not_applicable_and_absent_cutover_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = home / "Applications" / "Agent Runtime.app"
            transaction = (
                home
                / "Library"
                / "Application Support"
                / "Agent Runtime"
                / "cutover-transaction"
            )
            self.assertEqual(doctor._installed_package_check(app).status, "not_applicable")
            self.assertEqual(doctor._service_registration_check(app).status, "not_applicable")
            cutover = doctor._cutover_identity_check(transaction, app)
            self.assertEqual(cutover.status, "pass")
            self.assertFalse(cutover.evidence["transaction_present"])

    def test_valid_installed_package_uses_zero_cost_substrate_and_external_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            with patch.object(doctor.package_provenance, "_verify_codesign", return_value=None):
                with patch.object(
                    doctor.package_provenance,
                    "zero_cost_responsible_code_identity",
                    return_value={"signing_mode": "adhoc", "team_identifier": None, "responsible_code": {}},
                ):
                    check = doctor._installed_package_check(app)
            self.assertEqual(check.status, "pass")
            self.assertEqual(check.evidence["substrate_revision"], "a" * 40)
            self.assertEqual(check.evidence["git_tree"], "b" * 40)
            self.assertEqual(check.evidence["payload_revision"], "d" * 40)
            self.assertIsNone(check.evidence["team_identifier"])

    def test_embedded_first_party_source_is_rejected_from_immutable_substrate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            embedded = app / "Contents" / "Resources" / "runtime" / "agent_runtime"
            embedded.mkdir()
            (embedded / "server.py").write_text("print('forbidden')\n")
            with patch.object(doctor.package_provenance, "_verify_codesign", return_value=None):
                check = doctor._installed_package_check(app)
            self.assertEqual(check.status, "fail")
            self.assertEqual(check.reason_code, "INSTALLED_SUBSTRATE_INVALID")

    def test_foreign_or_malformed_installed_package_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            info_path = app / "Contents" / "Info.plist"
            info = plistlib.loads(info_path.read_bytes())
            info["CFBundleIdentifier"] = "com.example.foreign"
            info_path.write_bytes(plistlib.dumps(info))
            check = doctor._installed_package_check(app)
            self.assertEqual(check.status, "fail")
            self.assertEqual(check.reason_code, "INSTALLED_SUBSTRATE_INVALID")

    def test_missing_selected_payload_has_actionable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            state = home / "Library" / "Application Support" / "Agent Runtime"
            closure = (state / "current-payload").read_text().strip()
            release = state / "payloads" / closure
            for directory in (release / "agent_runtime", release):
                directory.chmod(0o755)
            for child in (release / "agent_runtime").iterdir():
                child.chmod(0o644)
            (release / doctor.package_provenance.PAYLOAD_MANIFEST_NAME).chmod(0o644)
            shutil.rmtree(release)
            with patch.object(doctor.package_provenance, "_verify_codesign", return_value=None):
                with patch.object(
                    doctor.package_provenance,
                    "zero_cost_responsible_code_identity",
                    return_value={"signing_mode": "adhoc", "team_identifier": None, "responsible_code": {}},
                ):
                    check = doctor._installed_package_check(app)
            self.assertEqual(check.status, "fail")
            self.assertEqual(check.reason_code, "PAYLOAD_RELEASE_MISSING")

    def test_lifecycle_ownership_validates_exact_current_launchagent_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            check = doctor._service_registration_check(app)
            self.assertEqual(check.status, "pass")
            self.assertEqual(check.reason_code, "OK")
            self.assertEqual(check.evidence["contract"], doctor.package_provenance.LIFECYCLE_CONTRACT)

            plist = doctor.candidate_cutover._current_launchagent_path(home)
            value = plistlib.loads(plist.read_bytes())
            value["Label"] = "com.example.foreign"
            plist.write_bytes(plistlib.dumps(value))
            failed = doctor._service_registration_check(app)
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.reason_code, "LIFECYCLE_OWNERSHIP_INVALID")

    def test_lifecycle_ownership_fails_when_main_app_login_registration_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            with patch.object(
                doctor.candidate_cutover,
                "_service_management",
                return_value={"main_app": "not-found", "runtime_agent": "not-found"},
            ):
                failed = doctor._service_registration_check(app)
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.reason_code, "MAIN_APP_REGISTRATION_ABSENT")

    def test_lifecycle_ownership_rejects_competing_runtime_service_management(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            with patch.object(
                doctor.candidate_cutover,
                "_service_management",
                return_value={"main_app": "enabled", "runtime_agent": "enabled"},
            ):
                failed = doctor._service_registration_check(app)
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.reason_code, "LIFECYCLE_OWNERSHIP_CONTRADICTORY")

    def test_cutover_schema5_valid_and_contradictory_states(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = home / "Applications" / "Agent Runtime.app"
            transaction = (
                home
                / "Library"
                / "Application Support"
                / "Agent Runtime"
                / "cutover-transaction"
            )
            transaction.mkdir(parents=True)
            metadata = {
                "schema": doctor.candidate_cutover.TRANSACTION_SCHEMA,
                "modern_runtime_label": doctor.candidate_cutover.MODERN_RUNTIME_LABEL,
                "status": "PENDING",
                "phase": "APP_SWAPPED",
                "candidate_validation": {
                    "mode": "strict",
                    "expected_candidate_sha256": None,
                    "expected_handoff_sha256": None,
                },
                "paths": {"target_app": str(app)},
            }
            path = transaction / "metadata.json"
            path.write_text(json.dumps(metadata), encoding="utf-8")

            mutation_names = (
                "cutover_candidate",
                "rollback_transaction",
                "recover_partial_transaction",
                "resume_transaction",
                "commit_transaction",
            )
            blockers = [
                patch.object(
                    doctor.candidate_cutover,
                    name,
                    side_effect=AssertionError(f"mutation attempted: {name}"),
                )
                for name in mutation_names
            ]
            for blocker in blockers:
                blocker.start()
                self.addCleanup(blocker.stop)

            check = doctor._cutover_identity_check(transaction, app)
            self.assertEqual(check.status, "warn")
            self.assertEqual(check.reason_code, "CUTOVER_TRANSACTION_PRESENT")
            self.assertEqual(check.evidence["phase"], "APP_SWAPPED")

            metadata["paths"]["target_app"] = str(home / "wrong.app")
            path.write_text(json.dumps(metadata), encoding="utf-8")
            failed = doctor._cutover_identity_check(transaction, app)
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.reason_code, "CUTOVER_STATE_INVALID")

class DoctorReportTests(unittest.TestCase):
    def test_collect_report_has_exact_inventory_and_composition(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            workspace = Path(raw) / "workspace"
            home.mkdir()
            workspace.mkdir()
            report = doctor.collect_report(home=home, environ=valid_env(workspace))
            self.assertEqual(report.status, "healthy")
            self.assertEqual(
                tuple(check.id for check in report.checks),
                doctor._CHECK_IDS,
            )
            self.assertEqual(len(report.checks), 10)
            self.assertEqual(doctor.exit_code(report), 0)

            bad_env = valid_env(workspace)
            bad_env["AGENT_RUNTIME_WORKSPACE_ROOT"] = "relative"
            unhealthy = doctor.collect_report(home=home, environ=bad_env)
            self.assertEqual(unhealthy.status, "unhealthy")
            self.assertEqual(doctor.exit_code(unhealthy), 1)

    def test_json_and_human_render_same_report_without_additional_probes(self) -> None:
        report = make_report("degraded")
        json_value = json.loads(doctor.render_json(report))
        human = doctor.render_human(report)
        self.assertEqual(json_value["status"], "degraded")
        self.assertEqual(json_value["checks"][0]["id"], "fixture")
        self.assertIn("fixture: warn", human)
        self.assertIn("Agent Runtime", human)

    def test_cli_json_is_one_object_deterministic_bounded_and_secret_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            env = os.environ.copy()
            env.update(valid_env(workspace))
            env["HOME"] = str(home)
            env["CONTROL_PLANE_API_KEY"] = "DO_NOT_LEAK_CONTROL_PLANE_SECRET"
            env["CONTROL_PLANE_TUNNEL_ID"] = "DO_NOT_LEAK_TUNNEL_SECRET"

            def run() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-m", "agent_runtime.doctor", "--json"],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=30,
                )

            first = run()
            second = run()
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first.stderr, "")
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(len(first.stdout.splitlines()), 1)
            parsed = json.loads(first.stdout)
            self.assertEqual(
                set(parsed),
                {"schema_version", "runtime_version", "status", "checks"},
            )
            self.assertEqual(parsed["status"], "healthy")
            self.assertLess(len(first.stdout.encode("utf-8")), 64 * 1024)
            self.assertNotIn("DO_NOT_LEAK_CONTROL_PLANE_SECRET", first.stdout)
            self.assertNotIn("DO_NOT_LEAK_TUNNEL_SECRET", first.stdout)

    def test_main_returns_two_only_for_internal_render_failure(self) -> None:
        report = make_report("healthy")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(doctor, "collect_report", return_value=report):
            with patch.object(doctor, "render_json", side_effect=ValueError("boom")):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = doctor.main(["--json"])
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["error"]["reason_code"],
            "INTERNAL_SERIALIZATION_FAILURE",
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_no_network_private_data_or_mutation_surface_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            env = valid_env(workspace)
            env["CONTROL_PLANE_API_KEY"] = "secret-control-plane"
            env["CONTROL_PLANE_TUNNEL_ID"] = "secret-tunnel"

            real_run = subprocess.run
            observed_argv: list[tuple[str, ...]] = []

            def fixed_process_only(argv, *args, **kwargs):
                observed_argv.append(tuple(str(item) for item in argv))
                if tuple(str(item) for item in argv) != ("/usr/bin/git", "--version"):
                    raise AssertionError(f"unexpected process: {argv!r}")
                return real_run(argv, *args, **kwargs)

            with patch.object(doctor.subprocess, "run", side_effect=fixed_process_only):
                with patch(
                    "socket.create_connection",
                    side_effect=AssertionError("network attempted"),
                ):
                    report = doctor.collect_report(home=home, environ=env)

            self.assertEqual(report.status, "healthy")
            self.assertEqual(observed_argv, [("/usr/bin/git", "--version")])
            rendered = doctor.render_json(report)
            self.assertNotIn("secret-control-plane", rendered)
            self.assertNotIn("secret-tunnel", rendered)

            source = (ROOT / "agent_runtime" / "doctor.py").read_text(encoding="utf-8")
            for forbidden in (
                "CONTROL_PLANE_API_KEY",
                "CONTROL_PLANE_TUNNEL_ID",
                "capture_screen(",
                ".ssh",
                "Keychain",
                "Photos",
                "launchctl",
                "git fetch",
                "git push",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
