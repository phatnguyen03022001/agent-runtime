from __future__ import annotations

import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "macos" / "package_provenance.py"


def load_provenance():
    spec = importlib.util.spec_from_file_location("package_provenance_task0055", PROVENANCE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Task0055MacOSContractTests(unittest.TestCase):
    def test_service_metadata_uses_bundle_program_for_signed_runtime_helper(self) -> None:
        plist_path = ROOT / "macos" / "AppBundle" / "Library" / "LaunchAgents" / "com.picmao.agent-runtime-runtime.plist"
        self.assertTrue(plist_path.is_file())
        payload = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(payload["Label"], "com.picmao.agent-runtime-runtime")
        self.assertEqual(payload["BundleProgram"], "Contents/MacOS/AgentRuntimeRuntimeService")
        self.assertNotIn("Program", payload)
        self.assertNotIn("AssociatedBundleIdentifiers", payload)
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertIs(payload["RunAtLoad"], False)

    def test_package_script_requires_explicit_non_adhoc_identity_without_keychain_discovery(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        self.assertIn("AGENT_RUNTIME_CODESIGN_IDENTITY", package)
        self.assertIn('[[ -n "$SIGNING_IDENTITY"', package)
        self.assertIn('--sign "$SIGNING_IDENTITY"', package)
        self.assertNotIn("--sign -", package)
        self.assertNotIn("security find-identity", package)
        self.assertNotIn("security find-certificate", package)

    def test_responsible_code_identity_requires_non_null_matching_team_identifier(self) -> None:
        provenance = load_provenance()
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "Agent Runtime.app"
            macos = app / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "com.picmao.agent-runtime",
                "CFBundleExecutable": "AgentRuntimeMenuBar",
            }))
            main = macos / "AgentRuntimeMenuBar"
            runtime = macos / "AgentRuntimeRuntimeService"
            main.write_bytes(b"main")
            runtime.write_bytes(b"runtime")
            result = provenance.responsible_code_identity(
                app, team_identifier_reader=lambda path: "TEAM123"
            )
            self.assertEqual(result["team_identifier"], "TEAM123")
            self.assertEqual(result["main_executable"], "Contents/MacOS/AgentRuntimeMenuBar")
            self.assertEqual(result["runtime_service_executable"], "Contents/MacOS/AgentRuntimeRuntimeService")

            with self.assertRaisesRegex(provenance.PackageProvenanceError, "TeamIdentifier"):
                provenance.responsible_code_identity(
                    app,
                    team_identifier_reader=lambda path: None if path.name == "AgentRuntimeRuntimeService" else "TEAM123",
                )
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "TeamIdentifier"):
                provenance.responsible_code_identity(
                    app,
                    team_identifier_reader=lambda path: "TEAM999" if path.name == "AgentRuntimeRuntimeService" else "TEAM123",
                )

    def test_install_surface_exposes_owner_safe_uninstall_and_retains_runtime_env(self) -> None:
        installer = (ROOT / "install.sh").read_text()
        self.assertIn("--uninstall", installer)
        self.assertIn("macos/uninstall.py", installer)
        uninstall_path = ROOT / "macos" / "uninstall.py"
        self.assertTrue(uninstall_path.is_file())
        source = uninstall_path.read_text()
        self.assertIn("runtime.env", source)
        for forbidden in ("resetbtm", "tccutil reset", "docker", "orb", "container cleanup"):
            self.assertNotIn(forbidden, source.lower())

    def test_readme_documents_task0055_runtime_ownership_and_authority_boundaries(self) -> None:
        readme = (ROOT / "README.md").read_text()
        required = (
            "bounded local execution provider",
            "MCP is the protocol",
            "transport",
            "app-owned ServiceManagement",
            "migration/rollback predecessor only",
            "requires-approval",
            "not-registered",
            "not-found",
            "operator/platform state",
            "no blanket TCC permissions",
            "Homebrew is optional",
            "AGENT_RUNTIME_CODESIGN_IDENTITY",
            "CPython 3.13",
            "runtime.env is retained by default",
            "build, package, candidate freeze, install/cutover, activation, update, rollback, uninstall, and cleanup",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, readme)



if __name__ == "__main__":
    unittest.main()
