from __future__ import annotations

import io
import json
import os
import plistlib
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
    runtime_package = runtime / "agent_runtime"
    macos = app / "Contents" / "MacOS"
    runtime_package.mkdir(parents=True)
    macos.mkdir(parents=True)

    (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (runtime_package / "server.py").write_text("print('ok')\n", encoding="utf-8")
    for name in (
        "AgentRuntimeMenuBar",
        "AgentRuntimeRuntimeService",
        "AgentRuntimeScreenCapture",
    ):
        path = macos / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    info = {
        "CFBundleIdentifier": doctor.package_provenance.OWNER,
        "CFBundleExecutable": "AgentRuntimeMenuBar",
        "CFBundleShortVersionString": RUNTIME_VERSION,
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    doctor.package_provenance.write_manifest(
        runtime,
        resources / "runtime-manifest.json",
        "a" * 40,
        "b" * 40,
        "c" * 64,
    )
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
        self.assertEqual(len(CAPABILITY_NAMES), 20)
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
        self.assertEqual(registry.evidence["screen_capture"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertEqual(governance.evidence["screen_capture"], "VISUAL_PERCEPTION_BLOCKED")

    def test_schema_check_rejects_task0148_schema_version_regression(self) -> None:
        bundle = doctor._schema_bundle()
        self.assertIsNotNone(bundle)
        for name, field, stale_value in (
            ("fs_read_batch", "result_schema_version", 1),
            ("fs_manage", "request_schema_version", 2),
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

    def test_valid_installed_package_uses_existing_provenance_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = make_installed_fixture(home)
            identity = {
                "team_identifier": "TEAM",
                "main_executable": doctor.package_provenance.MAIN_EXECUTABLE_RELATIVE,
                "runtime_service_executable": doctor.package_provenance.RUNTIME_SERVICE_EXECUTABLE_RELATIVE,
            }
            with patch.object(
                doctor.package_provenance,
                "validate_manifest",
                side_effect=AssertionError("full payload walk attempted"),
            ):
                with patch.object(
                    doctor.package_provenance,
                    "responsible_code_identity",
                    return_value=identity,
                ) as responsible:
                    with patch.object(
                        doctor.package_provenance,
                        "_verify_codesign",
                        return_value=None,
                    ) as codesign:
                        check = doctor._installed_package_check(app)
            self.assertEqual(check.status, "pass")
            self.assertEqual(check.evidence["runtime_revision"], "a" * 40)
            self.assertEqual(check.evidence["git_tree"], "b" * 40)
            responsible.assert_called_once_with(app)
            codesign.assert_called_once_with(app)

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
            self.assertEqual(check.reason_code, "INSTALLED_PACKAGE_INVALID")

    def test_service_registration_uses_status_only_and_classifies_states(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            app = home / "Applications" / "Agent Runtime.app"
            app.mkdir(parents=True)

            cases = (
                ({"main_app": "enabled", "runtime_agent": "enabled"}, "pass", "OK"),
                (
                    {"main_app": "enabled", "runtime_agent": "requires-approval"},
                    "warn",
                    "SERVICE_APPROVAL_REQUIRED",
                ),
                (
                    {"main_app": "not-registered", "runtime_agent": "enabled"},
                    "warn",
                    "SERVICE_NOT_REGISTERED",
                ),
            )
            for state, status, reason in cases:
                with self.subTest(state=state):
                    with patch.object(
                        doctor.candidate_cutover,
                        "_service_management",
                        return_value=state,
                    ) as service:
                        check = doctor._service_registration_check(app)
                    self.assertEqual(check.status, status)
                    self.assertEqual(check.reason_code, reason)
                    service.assert_called_once_with(app, "status")

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
