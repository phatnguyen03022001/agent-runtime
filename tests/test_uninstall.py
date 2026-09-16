from __future__ import annotations

import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL_PATH = ROOT / "macos" / "uninstall.py"
UI_LABEL = "com.picmao.agent-runtime-ui"
LEGACY_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
MODERN_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime-service"


def load_module():
    if not UNINSTALL_PATH.is_file():
        raise AssertionError("macos/uninstall.py must exist")
    spec = importlib.util.spec_from_file_location("agent_runtime_uninstall", UNINSTALL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_app(home: Path, state_file: Path, *, modern: bool) -> Path:
    app = home / "Applications" / "Agent Runtime.app"
    macos = app / "Contents" / "MacOS"
    runtime = app / "Contents" / "Resources" / "runtime"
    macos.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.picmao.agent-runtime",
        "CFBundleExecutable": "AgentRuntimeMenuBar",
    }))
    main = macos / "AgentRuntimeMenuBar"
    main.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"state = Path({str(state_file)!r})\n"
        "value = json.loads(state.read_text())\n"
        "if sys.argv[-1] == 'status': print(json.dumps(value, sort_keys=True))\n"
        "elif sys.argv[-1] == 'unregister':\n"
        "  if value['main_app'] in ('enabled', 'requires-approval'): value['main_app'] = 'not-registered'\n"
        "  if value['runtime_agent'] in ('enabled', 'requires-approval'): value['runtime_agent'] = 'not-registered'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "  print(json.dumps(value, sort_keys=True))\n"
        "elif sys.argv[-1] == 'register':\n"
        "  if value['main_app'] in ('not-found', 'not-registered'): value['main_app'] = 'enabled'\n"
        "  if value['runtime_agent'] in ('not-found', 'not-registered'): value['runtime_agent'] = 'enabled'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "  print(json.dumps(value, sort_keys=True))\n"
        "elif sys.argv[-1] == 'register-main':\n"
        "  if value['main_app'] in ('not-found', 'not-registered'): value['main_app'] = 'enabled'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "  print(json.dumps(value, sort_keys=True))\n"
        "elif sys.argv[-1] == 'register-runtime':\n"
        "  if value['runtime_agent'] in ('not-found', 'not-registered'): value['runtime_agent'] = 'enabled'\n"
        "  state.write_text(json.dumps(value) + '\\n')\n"
        "  print(json.dumps(value, sort_keys=True))\n"
        "else: raise SystemExit(2)\n"
    )
    main.chmod(0o755)
    (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n")
    if modern:
        plist = app / "Contents" / "Library" / "LaunchAgents" / f"{MODERN_RUNTIME_LABEL}.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(plistlib.dumps({
            "Label": MODERN_RUNTIME_LABEL,
            "BundleProgram": "Contents/MacOS/AgentRuntimeRuntimeService",
        }))
        helper = macos / "AgentRuntimeRuntimeService"
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
    return app


def write_legacy_plist(path: Path, label: str, app: Path) -> None:
    if label == UI_LABEL:
        args = [str(app / "Contents" / "MacOS" / "AgentRuntimeMenuBar")]
    else:
        args = [str(app / "Contents" / "Resources" / "runtime" / "start.sh"), "--serve", "/usr/local/bin/tunnel-client"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args}))


def make_absent_launchctl(root: Path) -> Path:
    script = root / "launchctl"
    script.write_text("#!/bin/sh\nexit 113\n")
    script.chmod(0o755)
    return script


class UninstallTests(unittest.TestCase):
    def fixture(self, raw: str, *, modern: bool = True):
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        service_state = root / "service-state.json"
        service_state.write_text(json.dumps({"main_app": "enabled", "runtime_agent": "requires-approval"}) + "\n")
        app = make_app(home, service_state, modern=modern)
        state_dir = home / "Library" / "Application Support" / "Agent Runtime"
        state_dir.mkdir(parents=True)
        env = state_dir / "runtime.env"
        env.write_bytes(b"CONTROL_PLANE_API_KEY=retained-secret\n")
        desired = state_dir / "protected-runtime-running"
        desired.write_text("")
        audit = state_dir / "protected-attempts.json"
        audit.write_text("{}\n")
        unrelated = state_dir / "operator-notes.txt"
        unrelated.write_text("keep\n")
        return root, home, service_state, app, state_dir, env, desired, audit, unrelated

    def test_modern_uninstall_unregisters_owned_services_removes_product_state_and_retains_config(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            _, home, service_state, app, _, env, desired, audit, unrelated = self.fixture(raw)
            before = env.read_bytes()
            result = uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(Path(raw)), uid=501)
            self.assertFalse(app.exists())
            self.assertFalse(desired.exists())
            self.assertFalse(audit.exists())
            self.assertTrue(unrelated.is_file())
            self.assertEqual(env.read_bytes(), before)
            self.assertEqual(result["retained_configuration"], str(env))
            self.assertEqual(result["configuration_removed"], False)
            self.assertEqual(
                json.loads(service_state.read_text()),
                {"main_app": "not-registered", "runtime_agent": "not-registered"},
            )

    def test_modern_uninstall_accepts_services_that_were_never_registered(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root, home, service_state, app, _, env, _, _, _ = self.fixture(raw)
            service_state.write_text(
                json.dumps({"main_app": "not-found", "runtime_agent": "not-registered"}) + "\n"
            )
            before = service_state.read_bytes()
            result = uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(root), uid=501)
            self.assertFalse(app.exists())
            self.assertTrue(env.is_file())
            self.assertEqual(service_state.read_bytes(), before)
            self.assertEqual(result["status"], "UNINSTALLED")

    def test_modern_uninstall_unregisters_registered_runtime_when_main_was_never_registered(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root, home, service_state, app, _, env, _, _, _ = self.fixture(raw)
            service_state.write_text(
                json.dumps({"main_app": "not-found", "runtime_agent": "enabled"}) + "\n"
            )
            uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(root), uid=501)
            self.assertFalse(app.exists())
            self.assertTrue(env.is_file())
            self.assertEqual(
                json.loads(service_state.read_text()),
                {"main_app": "not-found", "runtime_agent": "not-registered"},
            )

    def test_pending_transaction_refuses_before_any_mutation(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            _, home, service_state, app, state_dir, env, desired, _, _ = self.fixture(raw)
            transaction = state_dir / "cutover-transaction"
            transaction.mkdir()
            before = (service_state.read_bytes(), env.read_bytes(), desired.exists())
            with self.assertRaisesRegex(uninstall.UninstallError, "pending"):
                uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(Path(raw)), uid=501)
            self.assertTrue(app.is_dir())
            self.assertEqual((service_state.read_bytes(), env.read_bytes(), desired.exists()), before)

    def test_ambiguous_legacy_ownership_refuses_before_removal(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            _, home, _, app, state_dir, env, _, _, _ = self.fixture(raw, modern=False)
            launch_dir = home / "Library" / "LaunchAgents"
            ui = launch_dir / f"{UI_LABEL}.plist"
            runtime = launch_dir / f"{LEGACY_RUNTIME_LABEL}.plist"
            write_legacy_plist(ui, UI_LABEL, app)
            write_legacy_plist(runtime, LEGACY_RUNTIME_LABEL, app)
            payload = plistlib.loads(runtime.read_bytes())
            payload["ProgramArguments"][0] = "/tmp/foreign/start.sh"
            runtime.write_bytes(plistlib.dumps(payload))
            with self.assertRaisesRegex(uninstall.UninstallError, "ownership"):
                uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(Path(raw)), uid=501)
            self.assertTrue(app.is_dir())
            self.assertTrue(ui.is_file())
            self.assertTrue(runtime.is_file())
            self.assertTrue(env.is_file())
            self.assertTrue(state_dir.is_dir())


    def test_nonempty_lifecycle_lock_refuses_before_any_destructive_mutation(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            _, home, service_state, app, state_dir, env, desired, audit, _ = self.fixture(raw)
            lifecycle = state_dir / "lifecycle.lock"
            lifecycle.mkdir()
            (lifecycle / "owner").write_text("busy\n")
            before = (service_state.read_bytes(), env.read_bytes(), desired.read_bytes(), audit.read_bytes())
            with self.assertRaisesRegex(uninstall.UninstallError, "lifecycle lock"):
                uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(Path(raw)), uid=501)
            self.assertTrue(app.is_dir())
            self.assertTrue(lifecycle.is_dir())
            self.assertEqual(
                (service_state.read_bytes(), env.read_bytes(), desired.read_bytes(), audit.read_bytes()),
                before,
            )

    def test_symlinked_transient_state_refuses_before_service_unregistration(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root, home, service_state, app, state_dir, env, desired, _, _ = self.fixture(raw)
            desired.unlink()
            foreign = root / "foreign"
            foreign.write_text("keep\n")
            desired.symlink_to(foreign)
            before = service_state.read_bytes()
            with self.assertRaisesRegex(uninstall.UninstallError, "transient"):
                uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(root), uid=501)
            self.assertTrue(app.is_dir())
            self.assertTrue(desired.is_symlink())
            self.assertEqual(foreign.read_text(), "keep\n")
            self.assertEqual(service_state.read_bytes(), before)
            self.assertTrue(env.is_file())

    def test_legacy_bootout_failure_preserves_files_and_remaining_product_state(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root, home, _, app, _, env, desired, audit, _ = self.fixture(raw, modern=False)
            launch_dir = home / "Library" / "LaunchAgents"
            ui = launch_dir / f"{UI_LABEL}.plist"
            runtime = launch_dir / f"{LEGACY_RUNTIME_LABEL}.plist"
            write_legacy_plist(ui, UI_LABEL, app)
            write_legacy_plist(runtime, LEGACY_RUNTIME_LABEL, app)
            log = root / "launchctl.log"
            launchctl = root / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n"
                f"echo \"$*\" >> {log}\n"
                "case \"$1\" in\n"
                "  print)\n"
                f"    case \"$2\" in *{UI_LABEL}) program='{app / 'Contents/MacOS/AgentRuntimeMenuBar'}' ;;\n"
                f"      *{LEGACY_RUNTIME_LABEL}) program='{app / 'Contents/Resources/runtime/start.sh'}' ;; esac\n"
                "    printf 'program = %s\\n' \"$program\"; exit 0 ;;\n"
                f"  bootout) case \"$2\" in *{LEGACY_RUNTIME_LABEL}) echo 'simulated failure' >&2; exit 7 ;; *) exit 0 ;; esac ;;\n"
                "esac\n"
                "exit 2\n"
            )
            launchctl.chmod(0o755)

            with self.assertRaisesRegex(uninstall.UninstallError, "could not unregister owned legacy service"):
                uninstall.uninstall_product(home=home, launchctl=launchctl, uid=501)

            self.assertTrue(app.is_dir())
            self.assertTrue(ui.is_file())
            self.assertTrue(runtime.is_file())
            self.assertTrue(desired.is_file())
            self.assertTrue(audit.is_file())
            self.assertTrue(env.is_file())
            self.assertIn(f"bootout gui/501/{UI_LABEL}", log.read_text())
            self.assertIn(f"bootout gui/501/{LEGACY_RUNTIME_LABEL}", log.read_text())

    def test_modern_and_legacy_uninstall_compensates_if_second_legacy_bootout_fails(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root, home, service_state, app, _, env, desired, audit, _ = self.fixture(raw, modern=True)
            launch_dir = home / "Library" / "LaunchAgents"
            ui = launch_dir / f"{UI_LABEL}.plist"
            runtime = launch_dir / f"{LEGACY_RUNTIME_LABEL}.plist"
            write_legacy_plist(ui, UI_LABEL, app)
            write_legacy_plist(runtime, LEGACY_RUNTIME_LABEL, app)
            launch_state = root / "legacy-state.txt"
            launch_state.write_text(f"{UI_LABEL}\n{LEGACY_RUNTIME_LABEL}\n")
            log = root / "launchctl.log"
            launchctl = root / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n"
                f"echo \"$*\" >> {log}\n"
                f"state={str(launch_state)!r}\n"
                "case \"$1\" in\n"
                "  print)\n"
                "    label=${2##*/}; grep -Fxq \"$label\" \"$state\" || exit 113\n"
                f"    case \"$label\" in {UI_LABEL}) program='{app / 'Contents/MacOS/AgentRuntimeMenuBar'}' ;;\n"
                f"      {LEGACY_RUNTIME_LABEL}) program='{app / 'Contents/Resources/runtime/start.sh'}' ;; esac\n"
                "    printf 'program = %s\\n' \"$program\"; exit 0 ;;\n"
                "  bootout)\n"
                "    label=${2##*/}; "
                f"if [ \"$label\" = {LEGACY_RUNTIME_LABEL!r} ]; then echo 'simulated failure' >&2; exit 7; fi\n"
                "    grep -Fvx \"$label\" \"$state\" > \"$state.tmp\" || true; mv \"$state.tmp\" \"$state\"; exit 0 ;;\n"
                "  bootstrap) label=$(basename \"$3\" .plist); grep -Fxq \"$label\" \"$state\" || echo \"$label\" >> \"$state\"; exit 0 ;;\n"
                "esac\n"
                "exit 2\n"
            )
            launchctl.chmod(0o755)

            with self.assertRaisesRegex(uninstall.UninstallError, "could not unregister owned legacy service"):
                uninstall.uninstall_product(home=home, launchctl=launchctl, uid=501)

            self.assertTrue(app.is_dir())
            self.assertTrue(ui.is_file())
            self.assertTrue(runtime.is_file())
            self.assertTrue(desired.is_file())
            self.assertTrue(audit.is_file())
            self.assertTrue(env.is_file())
            self.assertEqual(
                json.loads(service_state.read_text()),
                {"main_app": "enabled", "runtime_agent": "enabled"},
            )
            self.assertIn(UI_LABEL, launch_state.read_text().splitlines())
            self.assertIn(LEGACY_RUNTIME_LABEL, launch_state.read_text().splitlines())
            log_text = log.read_text()
            self.assertIn(f"bootout gui/501/{UI_LABEL}", log_text)
            self.assertIn(f"bootout gui/501/{LEGACY_RUNTIME_LABEL}", log_text)
            self.assertIn(f"bootstrap gui/501 {ui}", log_text)

    def test_uninstall_compensation_restores_only_preexisting_runtime_registration(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root, home, service_state, app, _, env, desired, audit, _ = self.fixture(raw, modern=True)
            service_state.write_text(
                json.dumps({"main_app": "not-found", "runtime_agent": "enabled"}) + "\n"
            )
            launch_dir = home / "Library" / "LaunchAgents"
            ui = launch_dir / f"{UI_LABEL}.plist"
            runtime = launch_dir / f"{LEGACY_RUNTIME_LABEL}.plist"
            write_legacy_plist(ui, UI_LABEL, app)
            write_legacy_plist(runtime, LEGACY_RUNTIME_LABEL, app)
            launch_state = root / "legacy-state.txt"
            launch_state.write_text(f"{UI_LABEL}\n{LEGACY_RUNTIME_LABEL}\n")
            launchctl = root / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\n"
                f"state={str(launch_state)!r}\n"
                "case \"$1\" in\n"
                "  print)\n"
                "    label=${2##*/}; grep -Fxq \"$label\" \"$state\" || exit 113\n"
                f"    case \"$label\" in {UI_LABEL}) program='{app / 'Contents/MacOS/AgentRuntimeMenuBar'}' ;;\n"
                f"      {LEGACY_RUNTIME_LABEL}) program='{app / 'Contents/Resources/runtime/start.sh'}' ;; esac\n"
                "    printf 'program = %s\\n' \"$program\"; exit 0 ;;\n"
                "  bootout)\n"
                "    label=${2##*/}; "
                f"if [ \"$label\" = {LEGACY_RUNTIME_LABEL!r} ]; then exit 7; fi\n"
                "    grep -Fvx \"$label\" \"$state\" > \"$state.tmp\" || true; mv \"$state.tmp\" \"$state\"; exit 0 ;;\n"
                "  bootstrap) label=$(basename \"$3\" .plist); grep -Fxq \"$label\" \"$state\" || echo \"$label\" >> \"$state\"; exit 0 ;;\n"
                "esac\n"
                "exit 2\n"
            )
            launchctl.chmod(0o755)

            with self.assertRaisesRegex(uninstall.UninstallError, "could not unregister owned legacy service"):
                uninstall.uninstall_product(home=home, launchctl=launchctl, uid=501)

            self.assertTrue(app.is_dir())
            self.assertTrue(env.is_file())
            self.assertTrue(desired.is_file())
            self.assertTrue(audit.is_file())
            self.assertEqual(
                json.loads(service_state.read_text()),
                {"main_app": "not-found", "runtime_agent": "enabled"},
            )

    def test_verified_unloaded_legacy_remnants_are_removed_without_global_cleanup(self) -> None:
        uninstall = load_module()
        with tempfile.TemporaryDirectory() as raw:
            _, home, _, app, _, env, _, _, _ = self.fixture(raw, modern=False)
            launch_dir = home / "Library" / "LaunchAgents"
            ui = launch_dir / f"{UI_LABEL}.plist"
            runtime = launch_dir / f"{LEGACY_RUNTIME_LABEL}.plist"
            write_legacy_plist(ui, UI_LABEL, app)
            write_legacy_plist(runtime, LEGACY_RUNTIME_LABEL, app)
            result = uninstall.uninstall_product(home=home, launchctl=make_absent_launchctl(Path(raw)), uid=501)
            self.assertFalse(app.exists())
            self.assertFalse(ui.exists())
            self.assertFalse(runtime.exists())
            self.assertTrue(env.is_file())
            self.assertEqual(result["legacy_remnants_removed"], 2)


if __name__ == "__main__":
    unittest.main()
