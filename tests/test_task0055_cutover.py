from __future__ import annotations

import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_candidate_cutover import make_fake_launchctl

ROOT = Path(__file__).resolve().parents[1]
CUTOVER_PATH = ROOT / "macos" / "candidate_cutover.py"
UI_LABEL = "com.picmao.agent-runtime-ui"
RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"


def load_cutover():
    spec = importlib.util.spec_from_file_location("candidate_cutover_task0055", CUTOVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_legacy_app(app: Path) -> None:
    main = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
    start = app / "Contents" / "Resources" / "runtime" / "start.sh"
    main.parent.mkdir(parents=True)
    start.parent.mkdir(parents=True)
    main.write_text("legacy-main\n")
    start.write_text("#!/bin/sh\nexit 0\n")
    start.chmod(0o755)


def write_legacy_plists(ui: Path, runtime: Path, app: Path, desired: Path) -> tuple[bytes, bytes]:
    ui_payload = plistlib.dumps({
        "Label": UI_LABEL,
        "ProgramArguments": [str(app / "Contents/MacOS/AgentRuntimeMenuBar")],
        "RunAtLoad": True,
    })
    runtime_payload = plistlib.dumps({
        "Label": RUNTIME_LABEL,
        "ProgramArguments": [str(app / "Contents/Resources/runtime/start.sh"), "--serve", "/usr/bin/true"],
        "EnvironmentVariables": {"HOME": str(app.parents[2]), "PATH": "/usr/bin:/bin"},
        "KeepAlive": {"PathState": {str(desired): True}},
    })
    ui.parent.mkdir(parents=True, exist_ok=True)
    ui.write_bytes(ui_payload)
    runtime.write_bytes(runtime_payload)
    return ui_payload, runtime_payload


def make_modern_candidate(app: Path, service_state: Path) -> None:
    main = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
    helper = app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
    runtime = app / "Contents" / "Resources" / "runtime"
    service_plist = app / "Contents" / "Library" / "LaunchAgents" / f"{RUNTIME_LABEL}.plist"
    main.parent.mkdir(parents=True)
    runtime.mkdir(parents=True)
    service_plist.parent.mkdir(parents=True)
    main.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"state = Path({str(service_state)!r})\n"
        "value = json.loads(state.read_text())\n"
        "op = sys.argv[-1]\n"
        "if op == 'register':\n"
        "  if value['main_app'] in ('not-found', 'not-registered'): value['main_app'] = 'enabled'\n"
        "  if value['runtime_agent'] in ('not-found', 'not-registered'): value['runtime_agent'] = 'requires-approval'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "elif op == 'register-main':\n"
        "  if value['main_app'] in ('not-found', 'not-registered'): value['main_app'] = 'enabled'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "elif op == 'register-runtime':\n"
        "  if value['runtime_agent'] in ('not-found', 'not-registered'): value['runtime_agent'] = 'requires-approval'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "elif op == 'unregister':\n"
        "  if value['main_app'] in ('enabled', 'requires-approval'): value['main_app'] = 'not-registered'\n"
        "  if value['runtime_agent'] in ('enabled', 'requires-approval'): value['runtime_agent'] = 'not-registered'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "elif op == 'unregister-main':\n"
        "  if value['main_app'] in ('enabled', 'requires-approval'): value['main_app'] = 'not-registered'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "elif op == 'unregister-runtime':\n"
        "  if value['runtime_agent'] in ('enabled', 'requires-approval'): value['runtime_agent'] = 'not-registered'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "print(json.dumps(value, sort_keys=True))\n"
    )
    main.chmod(0o755)
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n")
    (runtime / "start.sh").chmod(0o755)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.picmao.agent-runtime",
        "CFBundleExecutable": "AgentRuntimeMenuBar",
    }))
    service_plist.write_bytes(plistlib.dumps({
        "Label": RUNTIME_LABEL,
        "BundleProgram": "Contents/MacOS/AgentRuntimeRuntimeService",
    }))


class ModernCutoverTests(unittest.TestCase):
    def fixture(self, raw: str):
        cutover = load_cutover()
        root = Path(raw)
        home = root / "home"
        target = home / "Applications" / "Agent Runtime.app"
        state_dir = home / "Library" / "Application Support" / "Agent Runtime"
        transaction = state_dir / "cutover-transaction"
        desired = state_dir / "protected-runtime-running"
        state_dir.mkdir(parents=True)
        desired.touch()
        runtime_env = state_dir / "runtime.env"
        runtime_env.write_text("CONTROL_PLANE_API_KEY=test-only\n")
        runtime_env.chmod(0o600)
        ui = home / "Library" / "LaunchAgents" / f"{UI_LABEL}.plist"
        runtime = home / "Library" / "LaunchAgents" / f"{RUNTIME_LABEL}.plist"
        make_legacy_app(target)
        ui_before, runtime_before = write_legacy_plists(ui, runtime, target, desired)
        candidate = root / "candidate" / "Agent Runtime.app"
        service_state = root / "services.json"
        service_state.write_text(json.dumps({"main_app": "not-found", "runtime_agent": "not-registered"}) + "\n")
        make_modern_candidate(candidate, service_state)
        handoff = root / "candidate.json"
        handoff.write_text("{}\n")
        launchctl, launch_state, launch_log = make_fake_launchctl(root, ui_loaded=True, runtime_loaded=True)
        programs_path = root / "launchctl-programs.json"
        programs = json.loads(programs_path.read_text())
        programs[f"gui/501/{UI_LABEL}"] = str(target / "Contents/MacOS/AgentRuntimeMenuBar")
        programs[f"gui/501/{RUNTIME_LABEL}"] = str(target / "Contents/Resources/runtime/start.sh")
        programs_path.write_text(json.dumps(programs, sort_keys=True) + "\n")
        expected = {
            "schema": 2,
            "bundle_identifier": "com.picmao.agent-runtime",
            "source_revision": "a" * 40,
            "source_tree": "b" * 40,
            "requirements_lock_sha256": "c" * 64,
            "team_identifier": "TEAM123",
            "main_executable": "Contents/MacOS/AgentRuntimeMenuBar",
            "runtime_service_executable": "Contents/MacOS/AgentRuntimeRuntimeService",
            "record_count": 1,
            "candidate_sha256": "d" * 64,
        }
        return cutover, locals()

    def run_cutover(self, cutover, fx, *, fail_stages=frozenset()):
        with mock.patch.object(cutover.provenance, "validate_candidate", return_value=fx["expected"]):
            return cutover.cutover_candidate(
                fx["candidate"], fx["handoff"], target_app=fx["target"],
                ui_plist=fx["ui"], runtime_plist=fx["runtime"], state_dir=fx["state_dir"],
                transaction_dir=fx["transaction"], home=fx["home"], launchctl=fx["launchctl"],
                tunnel_client=Path("/usr/bin/true"), uid=501, fail_stages=set(fail_stages),
            )

    def test_legacy_to_modern_migration_removes_dual_ownership_and_preserves_pending_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cutover, fx = self.fixture(raw)
            result = self.run_cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            self.assertFalse(fx["ui"].exists())
            self.assertFalse(fx["runtime"].exists())
            self.assertEqual(json.loads(fx["launch_state"].read_text()), [])
            self.assertEqual(
                json.loads(fx["service_state"].read_text()),
                {"main_app": "enabled", "runtime_agent": "requires-approval"},
            )
            self.assertTrue(fx["desired"].is_file())
            self.assertTrue(fx["transaction"].is_dir())

            with mock.patch.object(cutover.provenance, "validate_candidate", return_value=fx["expected"]):
                cutover.rollback_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertEqual(fx["ui"].read_bytes(), fx["ui_before"])
            self.assertEqual(fx["runtime"].read_bytes(), fx["runtime_before"])
            self.assertEqual(
                set(json.loads(fx["launch_state"].read_text())),
                {"gui/501/com.picmao.agent-runtime-ui", "gui/501/com.picmao.agent-runtime-runtime"},
            )
            self.assertEqual(
                json.loads(fx["service_state"].read_text()),
                {"main_app": "not-registered", "runtime_agent": "not-registered"},
            )
            self.assertTrue(fx["desired"].is_file())

    def test_modern_rollback_accepts_never_registered_main_app(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cutover, fx = self.fixture(raw)
            fx["target"].parent.mkdir(parents=True, exist_ok=True)
            if fx["target"].exists():
                import shutil
                shutil.rmtree(fx["target"])
            import shutil
            shutil.copytree(fx["candidate"], fx["target"])
            fx["transaction"].mkdir(parents=True, exist_ok=True)
            (fx["transaction"] / "candidate-handoff.json").write_text("{}\n")
            fx["service_state"].write_text(
                json.dumps({"main_app": "not-found", "runtime_agent": "not-registered"}) + "\n"
            )
            operations: list[str] = []
            original = cutover._service_management

            def observed(app: Path, operation: str):
                operations.append(operation)
                return original(app, operation)

            with mock.patch.object(cutover.provenance, "validate_candidate", return_value=fx["expected"]), \
                 mock.patch.object(cutover, "_service_management", side_effect=observed):
                cutover._unregister_modern_generation(
                    fx["transaction"], fx["target"],
                    registration_before={"main_app": "not-found", "runtime_agent": "not-registered"},
                )

            self.assertEqual(operations, ["status", "status"])

    def test_modern_rollback_unregisters_registered_runtime_when_main_was_never_registered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cutover, fx = self.fixture(raw)
            fx["target"].parent.mkdir(parents=True, exist_ok=True)
            if fx["target"].exists():
                import shutil
                shutil.rmtree(fx["target"])
            import shutil
            shutil.copytree(fx["candidate"], fx["target"])
            fx["transaction"].mkdir(parents=True, exist_ok=True)
            (fx["transaction"] / "candidate-handoff.json").write_text("{}\n")
            state = {"main_app": "not-found", "runtime_agent": "enabled"}
            operations: list[str] = []

            def service_management(_app: Path, operation: str):
                operations.append(operation)
                if operation == "status":
                    return dict(state)
                if operation == "unregister-runtime":
                    state["runtime_agent"] = "not-registered"
                    return dict(state)
                raise AssertionError(operation)

            with mock.patch.object(cutover.provenance, "validate_candidate", return_value=fx["expected"]), \
                 mock.patch.object(cutover, "_service_management", side_effect=service_management):
                cutover._unregister_modern_generation(
                    fx["transaction"], fx["target"],
                    registration_before={"main_app": "not-found", "runtime_agent": "not-registered"},
                )

            self.assertEqual(operations, ["status", "unregister-runtime", "status"])
            self.assertEqual(state, {"main_app": "not-found", "runtime_agent": "not-registered"})

    def test_post_registration_rollback_does_not_resurrect_stale_runtime_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cutover, fx = self.fixture(raw)
            fx["service_state"].write_text(
                json.dumps({"main_app": "not-found", "runtime_agent": "enabled"}) + "\n"
            )
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self.run_cutover(cutover, fx, fail_stages={"activation_refresh"})
            restored_state = json.loads(fx["service_state"].read_text())
            self.assertIn(restored_state["main_app"], {"not-found", "not-registered"})
            self.assertEqual(restored_state["runtime_agent"], "not-registered")
            self.assertEqual(fx["ui"].read_bytes(), fx["ui_before"])
            self.assertEqual(fx["runtime"].read_bytes(), fx["runtime_before"])

    def test_post_registration_failure_rolls_back_exact_legacy_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cutover, fx = self.fixture(raw)
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self.run_cutover(cutover, fx, fail_stages={"activation_refresh"})
            self.assertEqual(fx["ui"].read_bytes(), fx["ui_before"])
            self.assertEqual(fx["runtime"].read_bytes(), fx["runtime_before"])
            self.assertEqual(
                set(json.loads(fx["launch_state"].read_text())),
                {"gui/501/com.picmao.agent-runtime-ui", "gui/501/com.picmao.agent-runtime-runtime"},
            )
            self.assertFalse(fx["transaction"].exists())
            self.assertTrue(fx["desired"].is_file())


if __name__ == "__main__":
    unittest.main()
