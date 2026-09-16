from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import stat
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "macos" / "package_provenance.py"
CUTOVER_PATH = ROOT / "macos" / "candidate_cutover.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateClosureTests(unittest.TestCase):
    def _signed_app(
        self, provenance, root: Path, *, marker: str = "candidate", revision: str = "a" * 40
    ) -> Path:
        app = root / "Agent Runtime.app"
        macos = app / "Contents" / "MacOS"
        runtime = app / "Contents" / "Resources" / "runtime"
        services = app / "Contents" / "Library" / "LaunchAgents"
        (runtime / "agent_runtime").mkdir(parents=True)
        macos.mkdir(parents=True)
        services.mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.picmao.agent-runtime",
            "CFBundleExecutable": "AgentRuntimeMenuBar",
        }))
        executable = macos / "AgentRuntimeMenuBar"
        executable.write_text(f"#!/bin/sh\necho {marker}\n")
        executable.chmod(0o755)
        runtime_service = macos / "AgentRuntimeRuntimeService"
        runtime_service.write_text("#!/bin/sh\nexit 0\n")
        runtime_service.chmod(0o755)
        (services / "com.picmao.agent-runtime-runtime.plist").write_bytes(plistlib.dumps({
            "Label": "com.picmao.agent-runtime-runtime",
            "BundleProgram": "Contents/MacOS/AgentRuntimeRuntimeService",
        }))
        start = runtime / "start.sh"
        start.write_text("#!/bin/sh\nexit 0\n")
        start.chmod(0o755)
        (runtime / "agent_runtime" / "server.py").write_text("TOOLS = 6\n")
        provenance.write_manifest(
            runtime,
            app / "Contents" / "Resources" / "runtime-manifest.json",
            revision,
            "b" * 40,
            "c" * 64,
        )
        subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, capture_output=True)
        provenance._codesign_team_identifier = lambda _path: "TEAMTEST"
        return app

    def test_candidate_closure_matches_independent_records_and_mode_changes_digest(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_candidate")
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "Agent Runtime.app"
            first = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
            second = app / "Contents" / "Resources" / "runtime" / "start.sh"
            second.parent.mkdir(parents=True)
            first.parent.mkdir(parents=True)
            first.write_bytes(b"binary\n")
            second.write_bytes(b"#!/bin/sh\n")
            first.chmod(0o755)
            second.chmod(0o700)

            closure = provenance.candidate_closure(app)
            expected_records = []
            for target in sorted((first, second), key=lambda item: item.relative_to(app).as_posix().encode("utf-8")):
                relative = target.relative_to(app).as_posix()
                mode = stat.S_IMODE(target.stat().st_mode)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                expected_records.append(f"{relative}\t{mode:04o}\t{target.stat().st_size}\t{digest}\n")
            expected_digest = hashlib.sha256("".join(expected_records).encode("utf-8")).hexdigest()
            self.assertEqual(closure["record_count"], 2)
            self.assertEqual(closure["candidate_sha256"], expected_digest)

            original = closure["candidate_sha256"]
            second.chmod(0o755)
            self.assertNotEqual(provenance.candidate_closure(app)["candidate_sha256"], original)

    def test_candidate_closure_rejects_symlink_and_unsafe_relative_path(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_candidate_reject")
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "Agent Runtime.app"
            payload = app / "Contents" / "payload"
            payload.parent.mkdir(parents=True)
            payload.write_text("ok")
            (app / "Contents" / "link").symlink_to("payload")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "symlink"):
                provenance.candidate_closure(app)
        for unsafe in ("../escape", "a\tb", "a\rb", "a\nb", ".", "a/../b"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(provenance.PackageProvenanceError):
                    provenance.validate_candidate_relative_path(unsafe)

    def test_candidate_handoff_is_external_and_validates_embedded_provenance_without_checkout_head(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_handoff")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = self._signed_app(provenance, root)
            handoff = root / "candidate.json"
            sealed = provenance.seal_candidate(app, handoff)
            self.assertFalse((app / "candidate.json").exists())
            self.assertEqual(sealed["schema"], 2)
            self.assertEqual(sealed["team_identifier"], "TEAMTEST")
            self.assertEqual(sealed["bundle_identifier"], "com.picmao.agent-runtime")
            self.assertEqual(sealed["source_revision"], "a" * 40)
            self.assertEqual(sealed["source_tree"], "b" * 40)
            self.assertEqual(sealed["requirements_lock_sha256"], "c" * 64)
            self.assertEqual(sealed["candidate_sha256"], provenance.candidate_closure(app)["candidate_sha256"])
            validated = provenance.validate_candidate(app, handoff)
            self.assertEqual(validated, sealed)

    def test_candidate_validation_rejects_wrong_identity_and_post_seal_mutation(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_handoff_reject")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = self._signed_app(provenance, root)
            handoff = root / "candidate.json"
            provenance.seal_candidate(app, handoff)
            data = json.loads(handoff.read_text())
            data["source_revision"] = "0" * 40
            wrong = root / "wrong.json"
            wrong.write_text(json.dumps(data) + "\n")
            with self.assertRaises(provenance.PackageProvenanceError):
                provenance.validate_candidate(app, wrong)

            executable = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
            executable.chmod(0o700)
            with self.assertRaises(provenance.PackageProvenanceError):
                provenance.validate_candidate(app, handoff)

    def test_candidate_validation_is_write_free_after_seal(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_write_free")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = self._signed_app(provenance, root)
            handoff = root / "candidate.json"
            provenance.seal_candidate(app, handoff)
            before = provenance.candidate_closure(app)
            provenance.validate_candidate(app, handoff)
            provenance.validate_candidate(app, handoff)
            after = provenance.candidate_closure(app)
            self.assertEqual(after, before)
            self.assertFalse(any(app.rglob("*.pyc")))
            self.assertFalse(any(path.name == "__pycache__" for path in app.rglob("*")))

    def test_candidate_closure_includes_signature_and_rejects_every_sealed_file_shape_mutation(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_mutations")
        mutations = (
            ("added", lambda app: (app / "Contents" / "unexpected.txt").write_text("extra")),
            ("removed", lambda app: (app / "Contents" / "MacOS" / "AgentRuntimeMenuBar").unlink()),
            ("content", lambda app: (app / "Contents" / "Resources" / "runtime" / "agent_runtime" / "server.py").write_text("TOOLS = 7\n")),
            ("mode", lambda app: (app / "Contents" / "MacOS" / "AgentRuntimeMenuBar").chmod(0o700)),
            ("symlink", lambda app: (app / "Contents" / "link").symlink_to("Info.plist")),
            ("fifo", lambda app: os.mkfifo(app / "Contents" / "fifo")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                app = self._signed_app(provenance, root)
                handoff = root / "candidate.json"
                provenance.seal_candidate(app, handoff)
                records = [record for _, record in provenance._candidate_records(app)]
                self.assertTrue(any(record.startswith("Contents/_CodeSignature/") for record in records))
                mutate(app)
                with self.assertRaises(provenance.PackageProvenanceError):
                    provenance.validate_candidate(app, handoff)




def make_fake_launchctl(
    root: Path,
    *,
    ui_loaded: bool,
    runtime_loaded: bool,
    bootout_delay_prints: int = 0,
    bootout_never_absent: bool = False,
) -> tuple[Path, Path, Path]:
    state = root / "launchctl-state.json"
    pending = root / "launchctl-pending.json"
    programs = root / "launchctl-programs.json"
    log = root / "launchctl.log"
    loaded = []
    initial_programs = {}
    if ui_loaded:
        service = "gui/501/com.picmao.agent-runtime-ui"
        loaded.append(service)
        initial_programs[service] = "/previous/AgentRuntimeMenuBar"
    if runtime_loaded:
        service = "gui/501/com.picmao.agent-runtime-runtime"
        loaded.append(service)
        initial_programs[service] = "/previous/start.sh"
    state.write_text(json.dumps(sorted(loaded)) + "\n")
    pending.write_text("{}\n")
    programs.write_text(json.dumps(initial_programs, sort_keys=True) + "\n")
    script = root / "launchctl"
    script_lines = [
        "#!/usr/bin/env python3",
        "import json, plistlib, sys",
        "from pathlib import Path",
        f"STATE = Path({str(state)!r})",
        f"PENDING = Path({str(pending)!r})",
        f"PROGRAMS = Path({str(programs)!r})",
        f"LOG = Path({str(log)!r})",
        f"BOOTOUT_DELAY_PRINTS = {bootout_delay_prints}",
        f"BOOTOUT_NEVER_ABSENT = {bootout_never_absent!r}",
        "loaded = set(json.loads(STATE.read_text()))",
        "pending = json.loads(PENDING.read_text())",
        "programs = json.loads(PROGRAMS.read_text())",
        "args = sys.argv[1:]",
        "with LOG.open('a') as handle: handle.write(' '.join(args) + '\\n')",
        "rc = 0",
        "if args[0] == 'print':",
        "    service = args[1]",
        "    if service in pending and not BOOTOUT_NEVER_ABSENT:",
        "        if pending[service] <= 0:",
        "            pending.pop(service, None)",
        "            loaded.discard(service)",
        "        else:",
        "            pending[service] -= 1",
        "    if service in loaded:",
        "        label = service.rsplit('/', 1)[-1]",
        "        print(f'{service} = {{')",
        "        print(f'\tpath = /fake/{label}.plist')",
        "        print('\tstate = not running')",
        "        print(f'\tprogram = {programs[service]}')",
        "        print('}')",
        "    else:",
        "        print(f'Could not find service \"{service.rsplit(chr(47), 1)[-1]}\" in domain for user gui: 501', file=sys.stderr)",
        "        rc = 113",
        "elif args[0] == 'bootstrap':",
        "    data = plistlib.loads(Path(args[2]).read_bytes())",
        "    service = args[1] + '/' + data['Label']",
        "    if service in loaded:",
        "        print('Bootstrap failed: 37: Operation already in progress', file=sys.stderr)",
        "        rc = 37",
        "    else:",
        "        loaded.add(service)",
        "        programs[service] = data['ProgramArguments'][0]",
        "elif args[0] == 'bootout':",
        "    if args[1] in loaded and (BOOTOUT_DELAY_PRINTS or BOOTOUT_NEVER_ABSENT): pending[args[1]] = BOOTOUT_DELAY_PRINTS",
        "    else: loaded.discard(args[1])",
        "elif args[0] == 'kickstart': rc = 0 if args[-1] in loaded else 1",
        "else: rc = 2",
        "STATE.write_text(json.dumps(sorted(loaded)) + '\\n')",
        "PENDING.write_text(json.dumps(pending, sort_keys=True) + '\\n')",
        "PROGRAMS.write_text(json.dumps(programs, sort_keys=True) + '\\n')",
        "raise SystemExit(rc)",
    ]
    script.write_text("\n".join(script_lines) + "\n")
    script.chmod(0o755)
    return script, state, log


def make_prior_plist(path: Path, label: str, marker: str) -> bytes:
    payload = plistlib.dumps({"Label": label, "ProgramArguments": [f"/previous/{marker}"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def legacy_plist_bytes(
    target: Path, home: Path, tunnel_client: Path, desired_state: Path
) -> tuple[bytes, bytes]:
    ui = plistlib.dumps({
        "Label": "com.picmao.agent-runtime-ui",
        "AssociatedBundleIdentifiers": ["com.picmao.agent-runtime"],
        "ProgramArguments": [str(target / "Contents/MacOS/AgentRuntimeMenuBar")],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    })
    runtime = plistlib.dumps({
        "Label": "com.picmao.agent-runtime-runtime",
        "AssociatedBundleIdentifiers": ["com.picmao.agent-runtime"],
        "ProgramArguments": [
            str(target / "Contents/Resources/runtime/start.sh"),
            "--serve",
            str(tunnel_client),
        ],
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": False,
        "KeepAlive": {"PathState": {str(desired_state): True}},
        "ProcessType": "Interactive",
        "ThrottleInterval": 2,
    })
    return ui, runtime


class CandidateCutoverTests(unittest.TestCase):
    def test_current_cutover_does_not_generate_legacy_launchagents(self) -> None:
        source = CUTOVER_PATH.read_text()
        current = source[source.index("def cutover_candidate("):source.index("def commit_transaction(")]
        self.assertIn('_service_management(target_app, "register-main")', current)
        self.assertIn('_service_management(target_app, "register-runtime")', current)
        self.assertNotIn('_service_management(target_app, "register")', current)
        self.assertIn("_remove_legacy_predecessor(", current)
        self.assertNotIn("_ui_plist(", source)
        self.assertNotIn("_runtime_plist(", source)
        self.assertNotIn('"bootstrap"', current)

    def _fixture(
        self, raw: str, *, predecessor_revision: str = "a" * 40, aggregate_only_predecessor: bool = False
    ):
        provenance = load_module(PROVENANCE_PATH, "package_provenance_txn")
        cutover = load_module(CUTOVER_PATH, "candidate_cutover_test")
        root = Path(raw)
        home = root / "home"
        target = home / "Applications" / "Agent Runtime.app"
        ui_plist = home / "Library" / "LaunchAgents" / "com.picmao.agent-runtime-ui.plist"
        runtime_plist = home / "Library" / "LaunchAgents" / "com.picmao.agent-runtime-runtime.plist"
        state_dir = home / "Library" / "Application Support" / "Agent Runtime"
        transaction = state_dir / "cutover-transaction"
        desired = state_dir / "protected-runtime-running"
        state_dir.mkdir(parents=True)
        desired.touch()
        runtime_env = state_dir / "runtime.env"
        runtime_env.write_text("CONTROL_PLANE_API_KEY=super-secret\n")
        previous = CandidateClosureTests()._signed_app(
            provenance, root / "previous", marker="previous", revision=predecessor_revision
        )
        target.parent.mkdir(parents=True)
        shutil.copytree(previous, target, copy_function=shutil.copy2)
        ui_plist.parent.mkdir(parents=True, exist_ok=True)
        ui_before, runtime_before = legacy_plist_bytes(target, home, Path("/usr/bin/true"), desired)
        ui_plist.write_bytes(ui_before)
        runtime_plist.write_bytes(runtime_before)
        ui_plist.chmod(0o600)
        runtime_plist.chmod(0o600)
        candidate = CandidateClosureTests()._signed_app(provenance, root / "candidate", marker="candidate")
        handoff = root / "candidate.json"
        provenance.seal_candidate(candidate, handoff)
        provenance._verify_codesign = lambda _app: None
        cutover.provenance._verify_codesign = lambda _app: None
        cutover.provenance._codesign_team_identifier = lambda _path: "TEAMTEST"
        modern_state = {"main_app": "not-registered", "runtime_agent": "not-registered"}
        service_operations: list[tuple[str, str]] = []
        service_revision_operations: list[tuple[str, str]] = []
        launchctl, launch_state, launch_log = make_fake_launchctl(root, ui_loaded=True, runtime_loaded=True)
        programs_path = root / "launchctl-programs.json"
        initial_programs = json.loads(programs_path.read_text())
        initial_programs["gui/501/com.picmao.agent-runtime-ui"] = str(target / "Contents/MacOS/AgentRuntimeMenuBar")
        initial_programs["gui/501/com.picmao.agent-runtime-runtime"] = str(target / "Contents/Resources/runtime/start.sh")
        programs_path.write_text(json.dumps(initial_programs, sort_keys=True) + "\n")

        def set_modern_runtime_loaded(app: Path, loaded: bool) -> None:
            service = "gui/501/com.picmao.agent-runtime-runtime"
            services = set(json.loads(launch_state.read_text()))
            programs = json.loads(programs_path.read_text())
            if loaded:
                services.add(service)
                programs[service] = str(app / "Contents/MacOS/AgentRuntimeRuntimeService")
            else:
                services.discard(service)
                programs.pop(service, None)
            launch_state.write_text(json.dumps(sorted(services)) + "\n")
            programs_path.write_text(json.dumps(programs, sort_keys=True) + "\n")

        def service_management(app: Path, operation: str) -> dict[str, str]:
            service_operations.append((str(app), operation))
            manifest = json.loads((app / "Contents/Resources/runtime-manifest.json").read_text())
            app_revision = str(manifest.get("runtime_revision", "opaque"))
            service_revision_operations.append((app_revision, operation))
            if aggregate_only_predecessor and app_revision == predecessor_revision and operation in {
                "register-main", "register-runtime", "unregister-main", "unregister-runtime"
            }:
                raise cutover.CutoverError(f"ServiceManagement {operation} failed: exit 2")
            if operation == "register":
                modern_state.update(main_app="enabled", runtime_agent="enabled")
                set_modern_runtime_loaded(app, True)
            elif operation == "register-main":
                modern_state["main_app"] = "enabled"
            elif operation == "register-runtime":
                modern_state["runtime_agent"] = "enabled"
                set_modern_runtime_loaded(app, True)
            elif operation == "unregister":
                modern_state.update(main_app="not-registered", runtime_agent="not-registered")
                set_modern_runtime_loaded(app, False)
            elif operation == "unregister-main":
                modern_state["main_app"] = "not-registered"
            elif operation == "unregister-runtime":
                modern_state["runtime_agent"] = "not-registered"
                set_modern_runtime_loaded(app, False)
            elif operation != "status":
                raise AssertionError(operation)
            return dict(modern_state)

        cutover._service_management = service_management
        return provenance, cutover, {
            "root": root, "home": home, "target": target, "ui_plist": ui_plist,
            "runtime_plist": runtime_plist, "state_dir": state_dir, "transaction": transaction,
            "desired": desired, "runtime_env": runtime_env, "candidate": candidate, "handoff": handoff,
            "launchctl": launchctl, "launch_state": launch_state, "launch_log": launch_log,
            "ui_before": ui_before, "runtime_before": runtime_before, "modern_state": modern_state,
            "service_operations": service_operations, "service_revision_operations": service_revision_operations,
            "programs_path": programs_path,
            "set_modern_runtime_loaded": set_modern_runtime_loaded,
        }

    def _cutover(self, cutover, fx, *, fail_stages=frozenset()):
        return cutover.cutover_candidate(
            fx["candidate"], fx["handoff"], target_app=fx["target"],
            ui_plist=fx["ui_plist"], runtime_plist=fx["runtime_plist"],
            state_dir=fx["state_dir"], transaction_dir=fx["transaction"],
            home=fx["home"], launchctl=fx["launchctl"], tunnel_client=Path("/usr/bin/true"),
            uid=501, fail_stages=set(fail_stages),
        )

    def _set_launch_program(self, fx, service: str, program: Path | None) -> None:
        loaded = set(json.loads(fx["launch_state"].read_text()))
        programs = json.loads(fx["programs_path"].read_text())
        if program is None:
            loaded.discard(service)
            programs.pop(service, None)
        else:
            loaded.add(service)
            programs[service] = str(program)
        fx["launch_state"].write_text(json.dumps(sorted(loaded)) + "\n")
        fx["programs_path"].write_text(json.dumps(programs, sort_keys=True) + "\n")

    def _schema1_partial_fixture(self, provenance, cutover, fx) -> dict[str, object]:
        previous_closure = cutover._rollback_app_closure(fx["target"])
        fx["transaction"].mkdir(parents=True, mode=0o700)
        shutil.copytree(fx["target"], fx["transaction"] / "previous-app", copy_function=shutil.copy2)
        shutil.copy2(fx["ui_plist"], fx["transaction"] / "previous-ui.plist")
        shutil.copy2(fx["runtime_plist"], fx["transaction"] / "previous-runtime.plist")
        shutil.copy2(fx["handoff"], fx["transaction"] / "candidate-handoff.json")
        shutil.rmtree(fx["target"])
        shutil.copytree(fx["candidate"], fx["target"], copy_function=shutil.copy2)
        fx["ui_plist"].unlink()
        fx["runtime_plist"].unlink()
        fx["launch_state"].write_text("[]\n")
        fx["programs_path"].write_text("{}\n")
        fx["modern_state"].update(main_app="enabled", runtime_agent="not-found")
        metadata = {
            "schema": 1,
            "status": "PARTIAL",
            "candidate": json.loads(fx["handoff"].read_text()),
            "previous": {
                "app_present": True,
                "app_closure": previous_closure,
                "ui_loaded": True,
                "runtime_loaded": False,
                "desired_state_present": True,
                "ui_plist": {"present": True, "mode": 0o600},
                "runtime_plist": {"present": True, "mode": 0o600},
            },
            "paths": {
                "target_app": str(fx["target"]),
                "ui_plist": str(fx["ui_plist"]),
                "runtime_plist": str(fx["runtime_plist"]),
                "desired_state": str(fx["desired"]),
            },
            "modern_registration_before": {"main_app": "not-found", "runtime_agent": "enabled"},
            "modern_registration": {"main_app": "enabled", "runtime_agent": "enabled"},
            "last_error": "rollback incomplete: pre-existing modern ServiceManagement state changed during rollback: runtime_agent",
        }
        (fx["transaction"] / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return {"previous_closure": previous_closure, "metadata": metadata}

    def _schema2_preswap_partial_fixture(self, provenance, cutover, fx) -> dict[str, object]:
        helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
        helper.write_bytes(helper.read_bytes() + b"schema2-current-incident\n")
        provenance.seal_candidate(fx["candidate"], fx["handoff"])
        previous_closure = cutover._rollback_app_closure(fx["target"])
        previous_identity = cutover._runtime_bundle_identity(fx["target"])
        fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
        self._set_launch_program(fx, "gui/501/com.picmao.agent-runtime-runtime", None)
        fx["transaction"].mkdir(parents=True, mode=0o700)
        shutil.copytree(fx["target"], fx["transaction"] / "previous-app", copy_function=shutil.copy2)
        shutil.copytree(fx["candidate"], fx["transaction"] / "staged-candidate", copy_function=shutil.copy2)
        shutil.copy2(fx["ui_plist"], fx["transaction"] / "previous-ui.plist")
        shutil.copy2(fx["runtime_plist"], fx["transaction"] / "previous-runtime.plist")
        shutil.copy2(fx["handoff"], fx["transaction"] / "candidate-handoff.json")
        metadata = {
            "schema": 2,
            "status": "PARTIAL",
            "candidate": json.loads(fx["handoff"].read_text()),
            "previous": {
                "app_present": True,
                "app_closure": previous_closure,
                "ui_loaded": True,
                "runtime_loaded": False,
                "desired_state_present": True,
                "ui_plist": {"present": True, "mode": 0o600},
                "runtime_plist": {"present": True, "mode": 0o600},
            },
            "modern_ownership_before": {
                "main_app": "not-registered",
                "runtime": {
                    "registration_state": "enabled",
                    "classification": "stale-registered",
                    "loaded": False,
                    "loaded_program": "",
                    "legacy_label_loaded": False,
                    **previous_identity,
                },
            },
            "runtime_generation_changed": True,
            "operations": {
                "main_registered": False,
                "main_unregistered": False,
                "runtime_unregistered": False,
                "runtime_registered": False,
            },
            "paths": {
                "target_app": str(fx["target"]),
                "ui_plist": str(fx["ui_plist"]),
                "runtime_plist": str(fx["runtime_plist"]),
                "desired_state": str(fx["desired"]),
            },
            "last_error": "rollback incomplete: candidate identity does not match expected external handoff",
        }
        (fx["transaction"] / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return {"previous_closure": previous_closure, "metadata": metadata}

    def test_aggregate_only_predecessor_refresh_uses_bounded_aggregate_contract(self) -> None:
        old_revision = "4fbf5b1b0ef3708c8fff479ca6718344f3bfd3c0"
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(
                raw, predecessor_revision=old_revision, aggregate_only_predecessor=True
            )
            fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
            self._set_launch_program(fx, "gui/501/com.picmao.agent-runtime-runtime", None)
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["schema"], 3)
            self.assertEqual(metadata["predecessor_service_contract"], "aggregate-v1")
            old_ops = [op for revision, op in fx["service_revision_operations"] if revision == old_revision]
            self.assertIn("unregister", old_ops)
            self.assertNotIn("unregister-runtime", old_ops)
            self.assertNotIn("register-runtime", old_ops)
            self.assertFalse(metadata["operations"]["main_unregistered"])
            self.assertTrue(metadata["operations"]["runtime_unregistered"])
            self.assertTrue(metadata["operations"]["runtime_registered"])

    def test_unknown_predecessor_contract_fails_closed_before_service_mutation(self) -> None:
        unknown_revision = "d" * 40
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(
                raw, predecessor_revision=unknown_revision, aggregate_only_predecessor=True
            )
            fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
            self._set_launch_program(fx, "gui/501/com.picmao.agent-runtime-runtime", None)
            with self.assertRaisesRegex(cutover.CutoverError, "predecessor.*contract"):
                self._cutover(cutover, fx)
            non_status = [op for _, op in fx["service_operations"] if op != "status"]
            self.assertEqual(non_status, [])
            self.assertFalse(fx["transaction"].exists())

    def test_preswap_stale_aggregate_refresh_rolls_back_without_candidate_identity_check(self) -> None:
        old_revision = "4fbf5b1b0ef3708c8fff479ca6718344f3bfd3c0"
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(
                raw, predecessor_revision=old_revision, aggregate_only_predecessor=True
            )
            previous_closure = cutover._rollback_app_closure(fx["target"])
            fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
            self._set_launch_program(fx, "gui/501/com.picmao.agent-runtime-runtime", None)
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx, fail_stages={"after_predecessor_refresh"})
            self.assertEqual(cutover._rollback_app_closure(fx["target"]), previous_closure)
            self.assertFalse(fx["transaction"].exists())
            self.assertIn(fx["modern_state"]["runtime_agent"], {"not-found", "not-registered"})
            old_ops = [op for revision, op in fx["service_revision_operations"] if revision == old_revision]
            self.assertIn("unregister", old_ops)
            self.assertNotIn("register", old_ops)

    def test_preswap_aggregate_rollback_restores_healthy_preexisting_main_and_runtime(self) -> None:
        old_revision = "4fbf5b1b0ef3708c8fff479ca6718344f3bfd3c0"
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(
                raw, predecessor_revision=old_revision, aggregate_only_predecessor=True
            )
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                "gui/501/com.picmao.agent-runtime-runtime",
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"changed-generation\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx, fail_stages={"after_predecessor_refresh"})
            self.assertEqual(fx["modern_state"], {"main_app": "enabled", "runtime_agent": "enabled"})
            old_ops = [op for revision, op in fx["service_revision_operations"] if revision == old_revision]
            self.assertIn("unregister", old_ops)
            self.assertIn("register", old_ops)
            self.assertNotIn("register-main", old_ops)
            self.assertNotIn("register-runtime", old_ops)

    def test_transaction_phase_is_persisted_before_and_immediately_after_app_swap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            observed: list[str | None] = []
            original = cutover._atomic_json

            def capture(path: Path, value: dict[str, object]) -> None:
                if path.name == "metadata.json":
                    observed.append(value.get("phase"))
                original(path, value)

            cutover._atomic_json = capture
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("PRE_SWAP", observed)
            self.assertIn("APP_SWAPPED", observed)
            self.assertLess(observed.index("PRE_SWAP"), observed.index("APP_SWAPPED"))

    def test_schema2_current_preswap_partial_recovery_is_no_live_mutation_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            incident = self._schema2_preswap_partial_fixture(provenance, cutover, fx)
            ui_before = fx["ui_plist"].read_bytes()
            runtime_before = fx["runtime_plist"].read_bytes()
            loaded_before = fx["launch_state"].read_bytes()
            fx["service_operations"].clear()
            result = cutover.recover_partial_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "RECOVERED")
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(cutover._rollback_app_closure(fx["target"]), incident["previous_closure"])
            self.assertEqual(fx["ui_plist"].read_bytes(), ui_before)
            self.assertEqual(fx["runtime_plist"].read_bytes(), runtime_before)
            self.assertEqual(fx["launch_state"].read_bytes(), loaded_before)
            self.assertTrue(fx["desired"].exists())
            self.assertEqual(fx["service_operations"], [])

    def test_schema2_partial_recovery_rejects_nonzero_operation_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._schema2_preswap_partial_fixture(provenance, cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["operations"]["runtime_unregistered"] = True
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            with self.assertRaisesRegex(cutover.CutoverError, "schema-2.*operation|no-live-mutation"):
                cutover.recover_partial_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].is_dir())

    def test_preswap_phase_mismatch_with_candidate_installed_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._cutover(cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["schema"] = 3
            metadata["phase"] = "PRE_SWAP"
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            with self.assertRaisesRegex(cutover.CutoverError, "phase|previous.*closure"):
                cutover.rollback_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].exists())

    def test_postswap_phase_requires_candidate_identity_before_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._cutover(cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["schema"] = 3
            metadata["phase"] = "APP_SWAPPED"
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            shutil.rmtree(fx["target"])
            shutil.copytree(fx["transaction"] / "previous-app", fx["target"], copy_function=shutil.copy2)
            with self.assertRaisesRegex(cutover.CutoverError, "candidate|phase"):
                cutover.rollback_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].exists())

    def test_runtime_enabled_without_modern_job_is_stale_not_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            expected = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            self.assertEqual(classify("enabled", None, expected), "stale-registered")

    def test_runtime_enabled_with_exact_modern_job_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            expected = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            self.assertEqual(classify("enabled", expected, expected), "healthy-registered")

    def test_runtime_enabled_with_wrong_program_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            expected = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            with self.assertRaisesRegex(cutover.CutoverError, "program identity"):
                classify("enabled", fx["root"] / "foreign-helper", expected)

    def test_requires_approval_is_distinct_from_enabled_health(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            expected = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            self.assertEqual(classify("requires-approval", None, expected), "awaiting-approval")

    def test_changed_helper_generation_forces_runtime_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(
                raw, predecessor_revision="7077257837bdaf76ef1558fe78b900ec3af68788"
            )
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                "gui/501/com.picmao.agent-runtime-runtime",
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"changed-generation\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])
            try:
                result = self._cutover(cutover, fx)
            except cutover.CutoverError as exc:
                self.fail(f"changed helper must use transactional refresh, not fail legacy identity checks: {exc}")
            self.assertEqual(result["status"], "PENDING")
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertIn("unregister-runtime", operations)
            self.assertIn("register-runtime", operations)
            self.assertLess(operations.index("unregister-runtime"), operations.index("register-runtime"))

    def test_unchanged_healthy_runtime_is_not_destructively_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                "gui/501/com.picmao.agent-runtime-runtime",
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            try:
                result = self._cutover(cutover, fx)
            except cutover.CutoverError as exc:
                self.fail(f"unchanged healthy modern ownership must survive cutover: {exc}")
            self.assertEqual(result["status"], "PENDING")
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertNotIn("unregister-runtime", operations)
            self.assertNotIn("register-runtime", operations)
            self.assertNotIn("register", operations)

    def test_post_register_enabled_without_loaded_job_cannot_be_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["launch_state"].write_text(json.dumps(["gui/501/com.picmao.agent-runtime-ui"]) + "\n")
            fx["programs_path"].write_text(json.dumps({
                "gui/501/com.picmao.agent-runtime-ui": str(fx["target"] / "Contents/MacOS/AgentRuntimeMenuBar")
            }) + "\n")
            fx["modern_state"].update(main_app="not-found", runtime_agent="not-registered")
            operations: list[str] = []

            def service_management(_app: Path, operation: str) -> dict[str, str]:
                operations.append(operation)
                if operation == "register-main":
                    fx["modern_state"]["main_app"] = "enabled"
                elif operation == "register-runtime":
                    fx["modern_state"]["runtime_agent"] = "enabled"
                elif operation == "unregister-main":
                    fx["modern_state"]["main_app"] = "not-registered"
                elif operation == "register":
                    fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
                elif operation == "unregister-runtime":
                    fx["modern_state"]["runtime_agent"] = "not-registered"
                elif operation != "status":
                    raise AssertionError(operation)
                return dict(fx["modern_state"])

            cutover._service_management = service_management
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx)
            self.assertFalse(fx["transaction"].exists())

    def test_new_cutover_records_schema3_pre_swap_ownership_and_operation_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["schema"], 3)
            self.assertEqual(metadata["phase"], "APP_SWAPPED")
            self.assertIn("modern_ownership_before", metadata)
            self.assertIn("runtime", metadata["modern_ownership_before"])
            runtime = metadata["modern_ownership_before"]["runtime"]
            self.assertIn(runtime["classification"], {"healthy-registered", "stale-registered", "absent", "awaiting-approval"})
            self.assertIn("helper_sha256", runtime)
            self.assertIn("plist_sha256", runtime)
            self.assertEqual(
                set(metadata["operations"]),
                {"main_registered", "main_unregistered", "runtime_unregistered", "runtime_registered"},
            )

    def test_future_rollback_does_not_resurrect_stale_preexisting_runtime_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(
                raw, predecessor_revision="7077257837bdaf76ef1558fe78b900ec3af68788"
            )
            fx["modern_state"].update(main_app="not-found", runtime_agent="enabled")
            self._set_launch_program(fx, "gui/501/com.picmao.agent-runtime-runtime", None)
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"refresh-required\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx, fail_stages={"activation_refresh"})
            self.assertIn(fx["modern_state"]["runtime_agent"], {"not-found", "not-registered"})
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertIn("unregister-runtime", operations)
            self.assertEqual(operations.count("register-runtime"), 1)

    def test_schema1_partial_recovery_restores_exact_previous_state_without_stale_runtime_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            incident = self._schema1_partial_fixture(provenance, cutover, fx)
            recover = getattr(cutover, "recover_partial_transaction", None)
            self.assertIsNotNone(recover, "bounded partial recovery path is required")
            result = recover(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            self.assertEqual(result["status"], "RECOVERED")
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(cutover._rollback_app_closure(fx["target"]), incident["previous_closure"])
            self.assertEqual(fx["ui_plist"].read_bytes(), fx["ui_before"])
            self.assertEqual(fx["runtime_plist"].read_bytes(), fx["runtime_before"])
            self.assertEqual(set(json.loads(fx["launch_state"].read_text())), {"gui/501/com.picmao.agent-runtime-ui"})
            self.assertEqual(fx["modern_state"]["runtime_agent"], "not-found")

    def test_schema1_partial_recovery_unregisters_created_main_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._schema1_partial_fixture(provenance, cutover, fx)
            recover = getattr(cutover, "recover_partial_transaction", None)
            self.assertIsNotNone(recover, "bounded partial recovery path is required")
            fx["service_operations"].clear()
            recover(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertIn("unregister-main", operations)
            self.assertNotIn("unregister-runtime", operations)
            self.assertNotIn("register-runtime", operations)

    def test_schema1_partial_recovery_never_executes_service_management_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._schema1_partial_fixture(provenance, cutover, fx)
            recover = getattr(cutover, "recover_partial_transaction", None)
            self.assertIsNotNone(recover, "bounded partial recovery path is required")
            fx["service_operations"].clear()
            recover(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            self.assertTrue(fx["service_operations"])
            self.assertTrue(all("cutover-transaction/previous-app" not in app for app, _ in fx["service_operations"]))

    def test_failed_partial_recovery_retains_transaction_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._schema1_partial_fixture(provenance, cutover, fx)
            recover = getattr(cutover, "recover_partial_transaction", None)
            self.assertIsNotNone(recover, "bounded partial recovery path is required")
            with self.assertRaisesRegex(cutover.CutoverError, "recovery incomplete"):
                recover(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501,
                    fail_stages={"recover_restore_app"},
                )
            self.assertTrue(fx["transaction"].is_dir())
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "PARTIAL")
            self.assertIn("recovery incomplete", metadata["last_error"])

    def test_partial_recovery_rejects_unsupported_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._schema1_partial_fixture(provenance, cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["schema"] = 999
            metadata_path.write_text(json.dumps(metadata) + "\n")
            recover = getattr(cutover, "recover_partial_transaction", None)
            self.assertIsNotNone(recover, "bounded partial recovery path is required")
            with self.assertRaisesRegex(cutover.CutoverError, "schema"):
                recover(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)

    def test_operation_ledger_only_compensates_transaction_created_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            compensate = getattr(cutover, "_compensate_operation_ledger", None)
            self.assertIsNotNone(compensate, "operation-ledger compensation is required")
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            fx["service_operations"].clear()
            ledger = {
                "main_registered": True,
                "main_unregistered": False,
                "runtime_unregistered": False,
                "runtime_registered": False,
            }
            compensate(fx["target"], ledger)
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertEqual(operations, ["status", "unregister-main", "status"])
            self.assertEqual(fx["modern_state"]["runtime_agent"], "enabled")

    def test_cutover_stays_pending_blocks_second_cutover_and_commit_discards_rollback_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            secret_before = fx["runtime_env"].read_bytes()
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            self.assertTrue(fx["transaction"].is_dir())
            self.assertTrue((fx["transaction"] / "previous-app").is_dir())
            self.assertEqual(fx["runtime_env"].read_bytes(), secret_before)
            self.assertNotIn("super-secret", (fx["transaction"] / "metadata.json").read_text())
            for artifact in fx["transaction"].rglob("*"):
                if artifact.is_file() and not artifact.is_symlink():
                    self.assertNotIn(b"super-secret", artifact.read_bytes())
            provenance.validate_candidate(fx["target"], fx["handoff"])
            with self.assertRaisesRegex(cutover.CutoverError, "pending"):
                self._cutover(cutover, fx)
            cutover.commit_transaction(fx["transaction"], fx["target"])
            self.assertFalse(fx["transaction"].exists())
            provenance.validate_candidate(fx["target"], fx["handoff"])

    def test_manual_rollback_restores_previous_package_plists_loaded_state_and_desired_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            previous_closure = provenance.candidate_closure(fx["target"])
            self._cutover(cutover, fx)
            log_before_rollback = fx["launch_log"].read_text()
            cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            rollback_log = fx["launch_log"].read_text()[len(log_before_rollback):]
            self.assertIn("kickstart -k gui/501/com.picmao.agent-runtime-runtime", rollback_log)
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(provenance.candidate_closure(fx["target"]), previous_closure)
            self.assertEqual(fx["ui_plist"].read_bytes(), fx["ui_before"])
            self.assertEqual(fx["runtime_plist"].read_bytes(), fx["runtime_before"])
            self.assertTrue(fx["desired"].exists())
            loaded = set(json.loads(fx["launch_state"].read_text()))
            self.assertEqual(loaded, {"gui/501/com.picmao.agent-runtime-ui", "gui/501/com.picmao.agent-runtime-runtime"})

    def test_material_cutover_failures_auto_rollback_without_live_state(self) -> None:
        for stage in ("after_app_swap", "after_installed_validation", "launchagent_registration", "activation_refresh"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                provenance, cutover, fx = self._fixture(raw)
                previous_closure = provenance.candidate_closure(fx["target"])
                with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                    self._cutover(cutover, fx, fail_stages={stage})
                self.assertEqual(provenance.candidate_closure(fx["target"]), previous_closure)
                self.assertEqual(fx["ui_plist"].read_bytes(), fx["ui_before"])
                self.assertEqual(fx["runtime_plist"].read_bytes(), fx["runtime_before"])
                self.assertFalse(fx["transaction"].exists())

    def test_rollback_failure_retains_partial_transaction_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            with self.assertRaisesRegex(cutover.CutoverError, "rollback incomplete"):
                self._cutover(cutover, fx, fail_stages={"after_app_swap", "rollback_restore_app"})
            self.assertTrue(fx["transaction"].is_dir())
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "PARTIAL")
            self.assertIn("rollback", metadata["last_error"].lower())

    def test_first_install_failure_rolls_back_to_absent_package_launchagents_and_desired_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            shutil.rmtree(fx["target"])
            fx["ui_plist"].unlink()
            fx["runtime_plist"].unlink()
            fx["desired"].unlink()
            fx["launch_state"].write_text("[]\n")
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx, fail_stages={"activation_refresh"})
            self.assertFalse(fx["target"].exists())
            self.assertFalse(fx["ui_plist"].exists())
            self.assertFalse(fx["runtime_plist"].exists())
            self.assertFalse(fx["desired"].exists())
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(json.loads(fx["launch_state"].read_text()), [])

    def test_cutover_waits_for_delayed_service_absence_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            launchctl, launch_state, launch_log = make_fake_launchctl(
                fx["root"], ui_loaded=True, runtime_loaded=True, bootout_delay_prints=2,
            )
            fx.update(launchctl=launchctl, launch_state=launch_state, launch_log=launch_log)
            programs = json.loads(fx["programs_path"].read_text())
            programs["gui/501/com.picmao.agent-runtime-ui"] = str(fx["target"] / "Contents/MacOS/AgentRuntimeMenuBar")
            programs["gui/501/com.picmao.agent-runtime-runtime"] = str(fx["target"] / "Contents/Resources/runtime/start.sh")
            fx["programs_path"].write_text(json.dumps(programs, sort_keys=True) + "\n")
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            log = launch_log.read_text().splitlines()
            runtime_service = "gui/501/com.picmao.agent-runtime-runtime"
            bootout = log.index(f"bootout {runtime_service}")
            absence_checks = [line for line in log[bootout + 1:] if line == f"print {runtime_service}"]
            self.assertGreaterEqual(len(absence_checks), 3)
            self.assertFalse(any(line.startswith("bootstrap ") for line in log))

    def test_loaded_ui_registration_is_replaced_not_kickstarted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            log = fx["launch_log"].read_text().splitlines()
            ui_service = "gui/501/com.picmao.agent-runtime-ui"
            self.assertIn(f"bootout {ui_service}", log)
            self.assertNotIn(f"kickstart -k {ui_service}", log)
            self.assertFalse(any(line.startswith("bootstrap ") for line in log))
            self.assertFalse(fx["ui_plist"].exists())

    def test_service_absence_timeout_fails_closed_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            launchctl, _, log = make_fake_launchctl(
                fx["root"], ui_loaded=True, runtime_loaded=True, bootout_never_absent=True,
            )
            service = "gui/501/com.picmao.agent-runtime-runtime"
            cutover._require_launchctl_ok(
                cutover._run([str(launchctl), "bootout", service]),
                "could not unregister test service",
            )
            with self.assertRaisesRegex(cutover.CutoverError, "timed out waiting for LaunchAgent absence"):
                cutover._wait_for_service_absence(
                    launchctl, service, timeout_seconds=0.01, poll_interval_seconds=0.001,
                )
            self.assertFalse(any(line.startswith("bootstrap ") for line in log.read_text().splitlines()))

    def test_registered_service_identity_requires_exact_program_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            service = "gui/501/com.picmao.agent-runtime-runtime"
            with self.assertRaisesRegex(cutover.CutoverError, "identity mismatch"):
                cutover._require_service_identity(
                    fx["launchctl"], service, fx["target"] / "wrong/start.sh",
                )

    def test_launchctl_failure_diagnostics_are_bounded_and_redact_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, _ = self._fixture(raw)
            noisy = "x" * 10000
            result = subprocess.CompletedProcess(
                ["/bin/launchctl", "bootstrap", "gui/501", "/tmp/runtime.plist"],
                37,
                stdout="CONTROL_PLANE_API_KEY=super-secret\nAUTH_TOKEN => bearer-secret\nPASSWORD: password-secret trailing-secret-fragment\n" + noisy,
                stderr="Bootstrap failed: 37: Operation already in progress\n" + noisy,
            )
            with self.assertRaises(cutover.CutoverError) as raised:
                cutover._require_launchctl_ok(result, "could not register Runtime LaunchAgent")
            message = str(raised.exception)
            self.assertIn("operation=bootstrap", message)
            self.assertIn("returncode=37", message)
            self.assertIn("Operation already in progress", message)
            self.assertNotIn("super-secret", message)
            self.assertNotIn("bearer-secret", message)
            self.assertNotIn("password-secret", message)
            self.assertNotIn("trailing-secret-fragment", message)
            self.assertLess(len(message), 5000)

    def test_launchagent_registration_failure_occurs_after_ui_replacement_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx, fail_stages={"launchagent_registration"})
            log = fx["launch_log"].read_text()
            self.assertIn("bootout gui/501/com.picmao.agent-runtime-ui", log)
            self.assertIn("bootstrap gui/501", log)
            self.assertFalse(fx["transaction"].exists())

    def test_cutover_rejects_unowned_previous_launchagent_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            previous = provenance.candidate_closure(fx["target"])
            payload = plistlib.loads(fx["runtime_plist"].read_bytes())
            payload["Label"] = "com.example.unowned"
            fx["runtime_plist"].write_bytes(plistlib.dumps(payload))
            with self.assertRaisesRegex(cutover.CutoverError, "previous Runtime LaunchAgent"):
                self._cutover(cutover, fx)
            self.assertEqual(provenance.candidate_closure(fx["target"]), previous)
            self.assertFalse(fx["transaction"].exists())


    def _make_legacy_previous_app(self, provenance, fx) -> tuple[bytes, bytes]:
        manifest = fx["target"] / "Contents" / "Resources" / "runtime-manifest.json"
        manifest_bytes = b'{"legacy_runtime_manifest": true}\n'
        manifest.write_bytes(manifest_bytes)
        pycache = fx["target"] / "Contents" / "Resources" / "runtime" / "agent_runtime" / "__pycache__"
        pycache.mkdir()
        bytecode = pycache / "server.cpython-313.pyc"
        bytecode_bytes = b"legacy-runtime-generated-bytecode\n"
        bytecode.write_bytes(bytecode_bytes)
        with self.assertRaises(provenance.PackageProvenanceError):
            provenance.embedded_candidate_identity(fx["target"])
        return manifest_bytes, bytecode_bytes

    def test_legacy_previous_app_is_snapshotted_and_restored_opaquely(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            manifest_bytes, bytecode_bytes = self._make_legacy_previous_app(provenance, fx)
            previous_closure = provenance.candidate_closure(fx["target"])
            try:
                result = self._cutover(cutover, fx)
            except cutover.CutoverError as exc:
                self.fail(f"legacy previous app must be accepted as opaque rollback material: {exc}")
            self.assertEqual(result["status"], "PENDING")
            cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            self.assertEqual(provenance.candidate_closure(fx["target"]), previous_closure)
            self.assertEqual((fx["target"] / "Contents" / "Resources" / "runtime-manifest.json").read_bytes(), manifest_bytes)
            self.assertEqual((fx["target"] / "Contents" / "Resources" / "runtime" / "agent_runtime" / "__pycache__" / "server.cpython-313.pyc").read_bytes(), bytecode_bytes)

    def test_changed_rollback_snapshot_fails_before_replacing_current_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._cutover(cutover, fx)
            backup = fx["transaction"] / "previous-app" / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
            backup.write_bytes(backup.read_bytes() + b"tampered\n")
            with self.assertRaisesRegex(cutover.CutoverError, "rollback"):
                cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "PARTIAL")
            try:
                provenance.validate_candidate(fx["target"], fx["handoff"])
            except provenance.PackageProvenanceError as exc:
                self.fail(f"failed rollback must leave the current candidate intact: {exc}")

    def test_malformed_rollback_snapshot_metadata_fails_before_replacing_current_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._cutover(cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["previous"]["app_present"] = "yes"
            metadata_path.write_text(json.dumps(metadata) + "\n")
            with self.assertRaisesRegex(cutover.CutoverError, "rollback"):
                cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            metadata = json.loads(metadata_path.read_text())
            self.assertEqual(metadata["status"], "PARTIAL")
            try:
                provenance.validate_candidate(fx["target"], fx["handoff"])
            except provenance.PackageProvenanceError as exc:
                self.fail(f"malformed rollback metadata must leave the current candidate intact: {exc}")

    def test_prebuilt_install_entry_never_invokes_package_builder_or_resigns(self) -> None:
        text = (ROOT / "install.sh").read_text()
        branch_start = text.index("--install-prebuilt)")
        branch_end = text.index("--commit-cutover)", branch_start)
        branch = text[branch_start:branch_end]
        self.assertIn("run_cutover_helper cutover", branch)
        self.assertNotIn("package_app.sh", branch)
        self.assertNotIn("codesign", branch)
        self.assertNotIn("AGENT_RUNTIME_CODESIGN_IDENTITY", branch)

    def test_package_script_seals_external_candidate_only_after_final_integrity_checks(self) -> None:
        text = (ROOT / "macos" / "package_app.sh").read_text()
        sign = text.index('/usr/bin/codesign --force --deep --sign "$SIGNING_IDENTITY" "$APP"')
        verify = text.index('/usr/bin/codesign --verify --deep --strict "$APP"', sign)
        final_manifest = text.index('package_provenance.py" validate', verify)
        seal = text.index('package_provenance.py" seal', final_manifest)
        self.assertLess(sign, verify)
        self.assertLess(verify, final_manifest)
        self.assertLess(final_manifest, seal)
        self.assertIn('CANDIDATE_HANDOFF="$REPO_ROOT/build/Agent Runtime.candidate.json"', text)

    def test_default_install_composes_build_and_prebuilt_cutover_but_leaves_transaction_pending(self) -> None:
        text = (ROOT / "install.sh").read_text()
        build = text.index('"$ROOT/macos/package_app.sh"')
        prebuilt = text.index('"$ROOT/install.sh" --install-prebuilt "$SOURCE_APP" "$CANDIDATE_HANDOFF"', build)
        self.assertLess(build, prebuilt)
        self.assertNotIn('"$ROOT/install.sh" --commit-cutover', text[prebuilt:])
        self.assertIn('pending explicit commit', text.lower())
        self.assertIn('--commit-cutover', text)
        self.assertIn('--rollback-cutover', text)
        self.assertNotIn('validate_package() {', text)
        self.assertNotIn('STAGING_APP="$TARGET_APPS/.Agent Runtime.app.', text)
        self.assertNotIn('/usr/bin/codesign --verify --deep --strict "$app"', text)



if __name__ == "__main__":
    unittest.main()
