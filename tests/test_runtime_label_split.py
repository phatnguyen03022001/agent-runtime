from __future__ import annotations

import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
MODERN_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime-service"


def load_cutover():
    path = ROOT / "macos" / "candidate_cutover.py"
    spec = importlib.util.spec_from_file_location("candidate_cutover_label_split", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeLabelSplitTests(unittest.TestCase):
    def test_bundled_runtime_plist_uses_only_modern_identity(self) -> None:
        launch_agents = ROOT / "macos" / "AppBundle" / "Library" / "LaunchAgents"
        modern = launch_agents / f"{MODERN_RUNTIME_LABEL}.plist"
        legacy = launch_agents / f"{LEGACY_RUNTIME_LABEL}.plist"
        self.assertTrue(modern.is_file())
        self.assertFalse(legacy.exists())
        payload = plistlib.loads(modern.read_bytes())
        self.assertEqual(payload["Label"], MODERN_RUNTIME_LABEL)
        self.assertEqual(payload["BundleProgram"], "Contents/MacOS/AgentRuntimeRuntimeService")
        self.assertEqual([path.name for path in launch_agents.glob("*.plist")], [f"{MODERN_RUNTIME_LABEL}.plist"])

    def test_service_management_and_packaging_use_modern_plist_name(self) -> None:
        swift = (ROOT / "macos/Sources/AgentRuntimeMenuBar/ServiceManagementController.swift").read_text()
        package = (ROOT / "macos/package_app.sh").read_text()
        self.assertIn(f'runtimePlistName = "{MODERN_RUNTIME_LABEL}.plist"', swift)
        self.assertNotIn(f'runtimePlistName = "{LEGACY_RUNTIME_LABEL}.plist"', swift)
        self.assertIn(f"AppBundle/Library/LaunchAgents/{MODERN_RUNTIME_LABEL}.plist", package)
        self.assertIn(f"$CONTENTS/Library/LaunchAgents/{MODERN_RUNTIME_LABEL}.plist", package)
        self.assertNotIn(f"$CONTENTS/Library/LaunchAgents/{LEGACY_RUNTIME_LABEL}.plist", package)

    def test_current_lifecycle_targets_only_modern_label(self) -> None:
        start = (ROOT / "start.sh").read_text()
        installer = (ROOT / "install.sh").read_text()
        self.assertIn(MODERN_RUNTIME_LABEL, start)
        self.assertNotIn(f'LABEL="{LEGACY_RUNTIME_LABEL}"', start)
        self.assertIn(f'/{MODERN_RUNTIME_LABEL}"', installer)
        self.assertNotIn(f'/{LEGACY_RUNTIME_LABEL}"', installer)

    def test_modern_ownership_does_not_require_launchctl_program_rendering(self) -> None:
        source = (ROOT / "macos/candidate_cutover.py").read_text()
        self.assertIn("def _modern_service_present", source)
        section = source[source.index("def _modern_ownership_snapshot_from_state"):source.index("def _modern_ownership_snapshot(")]
        self.assertIn("_modern_service_present", section)
        self.assertNotIn("_loaded_service_program(launchctl, modern_runtime_service)", section)
        self.assertNotIn("_loaded_service_program(launchctl, modern_runtime_service)", source)

    def test_cutover_exposes_typed_legacy_and_modern_labels(self) -> None:
        cutover = load_cutover()
        self.assertEqual(cutover.LEGACY_RUNTIME_LABEL, LEGACY_RUNTIME_LABEL)
        self.assertEqual(cutover.MODERN_RUNTIME_LABEL, MODERN_RUNTIME_LABEL)

    def test_pure_legacy_predecessor_does_not_invoke_service_management(self) -> None:
        cutover = load_cutover()
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "Agent Runtime.app"
            main = target / "Contents/MacOS/AgentRuntimeMenuBar"
            main.parent.mkdir(parents=True)
            main.write_text("legacy-binary-placeholder\n")
            cutover._service_management = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("pure legacy predecessor must not invoke ServiceManagement")
            )
            cutover._loaded_service_program = lambda *_args, **_kwargs: None
            cutover._modern_service_present = lambda *_args, **_kwargs: False
            snapshot = cutover._modern_ownership_snapshot(
                target, launchctl=Path("/bin/launchctl"), uid=501, runtime_legacy_program=None
            )
            self.assertEqual(snapshot["main_app"], "not-found")
            self.assertEqual(snapshot["runtime"]["registration_state"], "not-found")
            self.assertEqual(snapshot["runtime"]["classification"], "absent")

    def test_legacy_loaded_job_does_not_satisfy_modern_health_identity(self) -> None:
        cutover = load_cutover()
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "Agent Runtime.app"
            legacy_program = target / "Contents/Resources/runtime/start.sh"
            calls: list[str] = []

            def loaded(_launchctl: Path, service: str):
                calls.append(service)
                if service.endswith("/" + LEGACY_RUNTIME_LABEL):
                    return legacy_program
                return None

            def modern_present(_launchctl: Path, service: str) -> bool:
                calls.append(service)
                return False

            cutover._loaded_service_program = loaded
            cutover._modern_service_present = modern_present
            snapshot = cutover._modern_ownership_snapshot_from_state(
                target,
                {"main_app": "not-found", "runtime_agent": "not-registered"},
                launchctl=Path("/bin/launchctl"),
                uid=501,
                runtime_legacy_program=legacy_program,
            )
            self.assertIn(f"gui/501/{LEGACY_RUNTIME_LABEL}", calls)
            self.assertIn(f"gui/501/{MODERN_RUNTIME_LABEL}", calls)
            self.assertTrue(snapshot["runtime"]["legacy_label_loaded"])
            self.assertFalse(snapshot["runtime"]["loaded"])
            self.assertEqual(snapshot["runtime"]["classification"], "absent")


if __name__ == "__main__":
    unittest.main()
