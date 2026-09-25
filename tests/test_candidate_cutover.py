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
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "macos" / "package_provenance.py"
CUTOVER_PATH = ROOT / "macos" / "candidate_cutover.py"
LEGACY_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
MODERN_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime-service"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateClosureTests(unittest.TestCase):
    def _signed_app(
        self, provenance, root: Path, *, marker: str = "candidate", revision: str = "a" * 40,
        runtime_label: str = MODERN_RUNTIME_LABEL,
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
        screen_capture = macos / "AgentRuntimeScreenCapture"
        screen_capture.write_text("#!/bin/sh\nexit 0\n")
        screen_capture.chmod(0o755)
        (services / f"{runtime_label}.plist").write_bytes(plistlib.dumps({
            "Label": runtime_label,
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

    def test_strict_candidate_validation_still_requires_strict_trust(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_strict_trust")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = self._signed_app(provenance, root)
            handoff = root / "candidate.json"
            provenance.seal_candidate(app, handoff)
            failure = provenance.PackageProvenanceError("synthetic strict trust failure")
            with mock.patch.object(provenance, "_verify_codesign", side_effect=failure) as strict:
                with self.assertRaisesRegex(provenance.PackageProvenanceError, "strict trust"):
                    provenance.validate_candidate(app, handoff)
            strict.assert_called_once_with(app)

    def test_pinned_candidate_validation_uses_exact_external_hashes_without_strict_trust(self) -> None:
        provenance = load_module(PROVENANCE_PATH, "package_provenance_pinned")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = self._signed_app(provenance, root)
            handoff = root / "candidate.json"
            sealed = provenance.seal_candidate(app, handoff)
            candidate_sha = sealed["candidate_sha256"]
            handoff_sha = hashlib.sha256(handoff.read_bytes()).hexdigest()

            strict = mock.Mock(side_effect=AssertionError("strict trust must not run"))
            provenance._verify_codesign = strict
            validated = provenance.validate_pinned_candidate(
                app, handoff, candidate_sha, handoff_sha
            )
            self.assertEqual(validated, sealed)
            strict.assert_not_called()

            with self.assertRaises(provenance.PackageProvenanceError):
                provenance.validate_pinned_candidate(app, handoff, "0" * 64, handoff_sha)
            with self.assertRaises(provenance.PackageProvenanceError):
                provenance.validate_pinned_candidate(app, handoff, candidate_sha, "0" * 64)
            for malformed in ("A" * 64, "a" * 63, "g" * 64):
                with self.subTest(identity="candidate", malformed=malformed):
                    with self.assertRaises(provenance.PackageProvenanceError):
                        provenance.validate_pinned_candidate(app, handoff, malformed, handoff_sha)
                with self.subTest(identity="handoff", malformed=malformed):
                    with self.assertRaises(provenance.PackageProvenanceError):
                        provenance.validate_pinned_candidate(app, handoff, candidate_sha, malformed)

            original_reader = provenance._codesign_team_identifier
            provenance._codesign_team_identifier = lambda _path: "OTHERTEAM"
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "external handoff"):
                provenance.validate_pinned_candidate(app, handoff, candidate_sha, handoff_sha)
            provenance._codesign_team_identifier = original_reader

            tx_handoff = root / "mutated-handoff.json"
            tx_handoff.write_bytes(handoff.read_bytes() + b" ")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "handoff SHA-256"):
                provenance.validate_pinned_candidate(app, tx_handoff, candidate_sha, handoff_sha)

            payload = app / "Contents" / "Resources" / "runtime" / "agent_runtime" / "server.py"
            payload.write_bytes(payload.read_bytes() + b"# drift\n")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "closure"):
                provenance.validate_pinned_candidate(app, handoff, candidate_sha, handoff_sha)
            strict.assert_not_called()

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
        "        if service in programs: print(f'\tprogram = {programs[service]}')",
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
        self, raw: str, *, predecessor_revision: str = "a" * 40, aggregate_only_predecessor: bool = False,
        previous_runtime_label: str = MODERN_RUNTIME_LABEL, candidate_runtime_label: str = MODERN_RUNTIME_LABEL,
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
        runtime_env.chmod(0o600)
        previous = CandidateClosureTests()._signed_app(
            provenance, root / "previous", marker="previous", revision=predecessor_revision,
            runtime_label=previous_runtime_label,
        )
        target.parent.mkdir(parents=True)
        shutil.copytree(previous, target, copy_function=shutil.copy2)
        ui_plist.parent.mkdir(parents=True, exist_ok=True)
        ui_before, runtime_before = legacy_plist_bytes(target, home, Path("/usr/bin/true"), desired)
        ui_plist.write_bytes(ui_before)
        runtime_plist.write_bytes(runtime_before)
        ui_plist.chmod(0o600)
        runtime_plist.chmod(0o600)
        candidate = CandidateClosureTests()._signed_app(
            provenance, root / "candidate", marker="candidate", runtime_label=candidate_runtime_label
        )
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
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
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

    def _cutover(
        self,
        cutover,
        fx,
        *,
        fail_stages=frozenset(),
        expected_candidate_sha256=None,
        expected_handoff_sha256=None,
    ):
        return cutover.cutover_candidate(
            fx["candidate"], fx["handoff"],
            expected_candidate_sha256=expected_candidate_sha256,
            expected_handoff_sha256=expected_handoff_sha256,
            target_app=fx["target"],
            ui_plist=fx["ui_plist"], runtime_plist=fx["runtime_plist"],
            state_dir=fx["state_dir"], transaction_dir=fx["transaction"],
            home=fx["home"], launchctl=fx["launchctl"],
            uid=501, fail_stages=set(fail_stages),
        )

    def _pins(self, fx):
        handoff = json.loads(fx["handoff"].read_text())
        return {
            "expected_candidate_sha256": handoff["candidate_sha256"],
            "expected_handoff_sha256": hashlib.sha256(fx["handoff"].read_bytes()).hexdigest(),
        }

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

    def _set_launch_service_without_program(self, fx, service: str, present: bool) -> None:
        loaded = set(json.loads(fx["launch_state"].read_text()))
        programs = json.loads(fx["programs_path"].read_text())
        if present:
            loaded.add(service)
            programs.pop(service, None)
        else:
            loaded.discard(service)
            programs.pop(service, None)
        fx["launch_state"].write_text(json.dumps(sorted(loaded)) + "\n")
        fx["programs_path"].write_text(json.dumps(programs, sort_keys=True) + "\n")

    def _approval_error(self, cutover, operation: str, state: dict[str, str]):
        error_type = getattr(cutover, "ServiceManagementApprovalRequired", None)
        self.assertIsNotNone(error_type, "typed ServiceManagement approval result is required")
        return error_type(operation, dict(state))

    def _awaiting_approval(self, cutover, fx, **cutover_kwargs):
        fx["modern_state"].update(main_app="not-registered", runtime_agent="not-registered")

        def service_management(app: Path, operation: str) -> dict[str, str]:
            fx["service_operations"].append((str(app), operation))
            if operation == "register-main":
                fx["modern_state"]["main_app"] = "enabled"
            elif operation == "register-runtime":
                raise self._approval_error(cutover, operation, fx["modern_state"])
            elif operation == "unregister-main":
                fx["modern_state"]["main_app"] = "not-registered"
            elif operation == "unregister-runtime":
                fx["modern_state"]["runtime_agent"] = "not-registered"
                fx["set_modern_runtime_loaded"](app, False)
            elif operation != "status":
                raise AssertionError(operation)
            return dict(fx["modern_state"])

        cutover._service_management = service_management
        return self._cutover(cutover, fx, **cutover_kwargs)

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
        previous_identity_current = cutover._runtime_bundle_identity(fx["target"])
        previous_identity = {
            key: previous_identity_current[key]
            for key in ("helper_program", "helper_sha256", "plist_sha256")
        }
        fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
        self._set_launch_program(fx, f"gui/501/{LEGACY_RUNTIME_LABEL}", None)
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

    def _schema4_awaiting_approval_fixture(self, raw: str):
        provenance, cutover, fx = self._fixture(
            raw, previous_runtime_label=LEGACY_RUNTIME_LABEL, candidate_runtime_label=LEGACY_RUNTIME_LABEL
        )
        previous_closure = cutover._rollback_app_closure(fx["target"])
        previous_helper = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
        previous_plist = fx["target"] / "Contents/Library/LaunchAgents" / f"{LEGACY_RUNTIME_LABEL}.plist"
        previous_identity = {
            "helper_program": str(previous_helper),
            "helper_sha256": hashlib.sha256(previous_helper.read_bytes()).hexdigest(),
            "plist_sha256": hashlib.sha256(previous_plist.read_bytes()).hexdigest(),
        }
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
        fx["modern_state"].update(main_app="enabled", runtime_agent="not-registered")
        self._set_launch_program(fx, f"gui/501/{LEGACY_RUNTIME_LABEL}", None)
        candidate = json.loads(fx["handoff"].read_text())
        candidate_helper = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
        candidate_plist = fx["target"] / "Contents/Library/LaunchAgents" / f"{LEGACY_RUNTIME_LABEL}.plist"
        candidate_identity = {
            "helper_program": str(candidate_helper),
            "helper_sha256": hashlib.sha256(candidate_helper.read_bytes()).hexdigest(),
            "plist_sha256": hashlib.sha256(candidate_plist.read_bytes()).hexdigest(),
        }
        metadata = {
            "schema": 4,
            "status": "AWAITING_APPROVAL",
            "phase": "APP_SWAPPED",
            "candidate": candidate,
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
                    "registration_state": "not-registered",
                    "classification": "absent",
                    "loaded": False,
                    "loaded_program": "",
                    "legacy_label_loaded": False,
                    **previous_identity,
                },
            },
            "runtime_generation_changed": True,
            "predecessor_service_contract": "unknown",
            "operations": {
                "main_registered": True,
                "main_unregistered": False,
                "runtime_unregistered": False,
                "runtime_registered": False,
                "runtime_approval_requested": True,
            },
            "runtime_config": {
                "path": str(fx["runtime_env"]),
                "mode": 0o600,
                "sha256": hashlib.sha256(fx["runtime_env"].read_bytes()).hexdigest(),
            },
            "paths": {
                "target_app": str(fx["target"]),
                "ui_plist": str(fx["ui_plist"]),
                "runtime_plist": str(fx["runtime_plist"]),
                "desired_state": str(fx["desired"]),
            },
            "modern_registration": {"main_app": "enabled", "runtime_agent": "not-registered"},
            "modern_ownership_after": {
                "main_app": "enabled",
                "runtime": {
                    "registration_state": "not-registered",
                    "classification": "absent",
                    "loaded": False,
                    "loaded_program": "",
                    "legacy_label_loaded": False,
                    **candidate_identity,
                },
            },
            "last_error": "",
        }
        (fx["transaction"] / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return provenance, cutover, fx, previous_closure

    def test_schema4_awaiting_approval_is_not_resumable_but_remains_rollbackable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx, previous_closure = self._schema4_awaiting_approval_fixture(raw)
            with self.assertRaisesRegex(cutover.CutoverError, "schema"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            fx["service_operations"].clear()
            cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(cutover._rollback_app_closure(fx["target"]), previous_closure)
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertNotIn("unregister-runtime", operations)

    def test_aggregate_only_predecessor_refresh_uses_bounded_aggregate_contract(self) -> None:
        old_revision = "4fbf5b1b0ef3708c8fff479ca6718344f3bfd3c0"
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(
                raw, predecessor_revision=old_revision, aggregate_only_predecessor=True
            )
            fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
            self._set_launch_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", None)
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["schema"], 5)
            self.assertEqual(metadata["modern_runtime_label"], MODERN_RUNTIME_LABEL)
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
            self._set_launch_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", None)
            with self.assertRaisesRegex(cutover.CutoverError, "predecessor.*contract"):
                self._cutover(cutover, fx)
            non_status = [op for _, op in fx["service_operations"] if op != "status"]
            self.assertEqual(non_status, [])
            self.assertFalse(fx["transaction"].exists())

    def test_split_v1_predecessor_revision_is_recognized(self) -> None:
        revision = "b5ac0ed5b461d3a259e4111e1a8f13933985f81f"
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw, predecessor_revision=revision)
            self.assertEqual(cutover._predecessor_service_contract(fx["target"]), "split-v1")

    def test_installed_predecessor_revision_is_recognized_as_split_v1(self) -> None:
        revisions = (
            "18cdb515fe037c9b6cb81ce6529d85ae734e195a",
            "4b9d7617d7dfbf297cf6e0776fdd1c8f6f58cc5b",
            "99518e694467be8525d813a4e638c4c8e0324365",
            "90a87fca8755ddc673e749bb5bf3e4489045a9b8",
            "89111218720ddb2f12961f9375549733edd23ada",
            "bf98ec4e9bcda97dfdc0ca52b8e0537dae14a1cc",
        )
        for revision in revisions:
            with self.subTest(revision=revision):
                with tempfile.TemporaryDirectory() as raw:
                    _, cutover, fx = self._fixture(raw, predecessor_revision=revision)
                    self.assertEqual(cutover._runtime_manifest_revision(fx["target"]), revision)
                    self.assertEqual(cutover._predecessor_service_contract(fx["target"]), "split-v1")

    def test_installed_split_v1_predecessor_refresh_uses_split_runtime_contract(self) -> None:
        exact_revision = "18cdb515fe037c9b6cb81ce6529d85ae734e195a"
        revisions = (
            "b0dec3e556ff914fb9ca041c6b52f53c04ee3fd2",
            "4b9d7617d7dfbf297cf6e0776fdd1c8f6f58cc5b",
            "99518e694467be8525d813a4e638c4c8e0324365",
            "90a87fca8755ddc673e749bb5bf3e4489045a9b8",
            "89111218720ddb2f12961f9375549733edd23ada",
            "bf98ec4e9bcda97dfdc0ca52b8e0537dae14a1cc",
            exact_revision,
        )
        for revision in revisions:
            with self.subTest(revision=revision):
                with tempfile.TemporaryDirectory() as raw:
                    provenance, cutover, fx = self._fixture(raw, predecessor_revision=revision)
                    self.assertEqual(cutover._runtime_manifest_revision(fx["target"]), revision)
                    self.assertEqual(cutover._predecessor_service_contract(fx["target"]), "split-v1")
                    fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
                    self._set_launch_program(
                        fx,
                        f"gui/501/{MODERN_RUNTIME_LABEL}",
                        fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
                    )
                    helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
                    helper.write_bytes(helper.read_bytes() + b"installed-split-refresh\n")
                    provenance.seal_candidate(fx["candidate"], fx["handoff"])

                    result = self._cutover(cutover, fx)

                    self.assertEqual(result["status"], "PENDING")
                    metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
                    self.assertEqual(metadata["predecessor_service_contract"], "split-v1")
                    predecessor_ops = [
                        operation
                        for app_revision, operation in fx["service_revision_operations"]
                        if app_revision == revision
                    ]
                    self.assertIn("unregister-runtime", predecessor_ops)
                    self.assertNotIn("unregister", predecessor_ops)
                    self.assertNotIn("register-runtime", predecessor_ops)

    def test_preswap_stale_aggregate_refresh_rolls_back_without_candidate_identity_check(self) -> None:
        old_revision = "4fbf5b1b0ef3708c8fff479ca6718344f3bfd3c0"
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(
                raw, predecessor_revision=old_revision, aggregate_only_predecessor=True
            )
            previous_closure = cutover._rollback_app_closure(fx["target"])
            fx["modern_state"].update(main_app="not-registered", runtime_agent="enabled")
            self._set_launch_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", None)
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
                f"gui/501/{MODERN_RUNTIME_LABEL}",
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

    def test_preswap_crash_after_predecessor_removal_restores_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            original_replace = os.replace

            def interrupt_staged_replace(source, destination) -> None:
                if Path(source) == fx["transaction"] / "staged-candidate" and Path(destination) == fx["target"]:
                    raise SystemExit("simulated process death before candidate placement")
                original_replace(source, destination)

            with mock.patch.object(cutover.os, "replace", side_effect=interrupt_staged_replace):
                with self.assertRaisesRegex(SystemExit, "before candidate placement"):
                    self._cutover(cutover, fx)

            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            previous_snapshot_valid = (
                cutover._rollback_app_closure(fx["transaction"] / "previous-app")
                == metadata["previous"]["app_closure"]
            )
            self.assertEqual(metadata["phase"], "PRE_SWAP")
            self.assertTrue(metadata["previous"]["app_present"])
            self.assertFalse(fx["target"].exists() or fx["target"].is_symlink())
            self.assertTrue((fx["transaction"] / "staged-candidate").is_dir())
            self.assertTrue(previous_snapshot_valid)
            state = (
                f"phase={metadata['phase']} previous.app_present={metadata['previous']['app_present']} "
                f"target_absent={not fx['target'].exists()} staged_present={(fx['transaction'] / 'staged-candidate').is_dir()} "
                f"previous_snapshot_valid={previous_snapshot_valid}"
            )
            try:
                result = cutover.rollback_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            except cutover.CutoverError as exc:
                self.fail(f"canonical rollback failed from reproduced crash state ({state}): {exc}")
            self.assertEqual(result["status"], "ROLLED_BACK")

    def test_preswap_crash_after_candidate_replace_before_phase_persist_restores_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            original_atomic_json = cutover._atomic_json

            def interrupt_app_swapped_persist(path: Path, value: dict[str, object]) -> None:
                if path.name == "metadata.json" and value.get("phase") == "APP_SWAPPED":
                    raise SystemExit("simulated process death before APP_SWAPPED persistence")
                original_atomic_json(path, value)

            cutover._atomic_json = interrupt_app_swapped_persist
            with self.assertRaisesRegex(SystemExit, "before APP_SWAPPED persistence"):
                self._cutover(cutover, fx)

            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            previous_snapshot_valid = (
                cutover._rollback_app_closure(fx["transaction"] / "previous-app")
                == metadata["previous"]["app_closure"]
            )
            installed_candidate = cutover.provenance.validate_candidate(
                fx["target"], fx["transaction"] / "candidate-handoff.json"
            )
            self.assertEqual(metadata["phase"], "PRE_SWAP")
            self.assertTrue(metadata["previous"]["app_present"])
            self.assertEqual(installed_candidate, metadata["candidate"])
            self.assertFalse((fx["transaction"] / "staged-candidate").exists())
            self.assertTrue(previous_snapshot_valid)
            state = (
                f"phase={metadata['phase']} previous.app_present={metadata['previous']['app_present']} "
                f"target_exact_candidate={installed_candidate == metadata['candidate']} staged_absent={not (fx['transaction'] / 'staged-candidate').exists()} "
                f"previous_snapshot_valid={previous_snapshot_valid}"
            )
            try:
                result = cutover.rollback_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            except cutover.CutoverError as exc:
                self.fail(f"canonical rollback failed from reproduced crash state ({state}): {exc}")
            self.assertEqual(result["status"], "ROLLED_BACK")

    def test_handled_replace_failure_after_predecessor_removal_auto_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            previous_closure = cutover._rollback_app_closure(fx["target"])
            original_replace = os.replace

            def fail_staged_replace(source, destination) -> None:
                if Path(source) == fx["transaction"] / "staged-candidate" and Path(destination) == fx["target"]:
                    raise OSError("simulated os.replace failure")
                original_replace(source, destination)

            with mock.patch.object(cutover.os, "replace", side_effect=fail_staged_replace):
                with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                    self._cutover(cutover, fx)
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(cutover._rollback_app_closure(fx["target"]), previous_closure)

    def test_first_install_preswap_candidate_crash_rolls_back_to_absent_app(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            shutil.rmtree(fx["target"])
            fx["ui_plist"].unlink()
            fx["runtime_plist"].unlink()
            fx["desired"].unlink()
            fx["launch_state"].write_text("[]\n")
            original_atomic_json = cutover._atomic_json

            def interrupt_app_swapped_persist(path: Path, value: dict[str, object]) -> None:
                if path.name == "metadata.json" and value.get("phase") == "APP_SWAPPED":
                    raise SystemExit("simulated first-install process death")
                original_atomic_json(path, value)

            cutover._atomic_json = interrupt_app_swapped_persist
            with self.assertRaisesRegex(SystemExit, "first-install"):
                self._cutover(cutover, fx)
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["phase"], "PRE_SWAP")
            self.assertFalse(metadata["previous"]["app_present"])
            cutover.provenance.validate_candidate(fx["target"], fx["transaction"] / "candidate-handoff.json")
            self.assertFalse((fx["transaction"] / "staged-candidate").exists())

            result = cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertFalse(fx["target"].exists() or fx["target"].is_symlink())
            self.assertFalse(fx["transaction"].exists())

    def test_preswap_symlink_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._cutover(cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["phase"] = "PRE_SWAP"
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            shutil.rmtree(fx["target"])
            fx["target"].symlink_to(fx["candidate"], target_is_directory=True)
            with self.assertRaisesRegex(cutover.CutoverError, "unsafe"):
                cutover.rollback_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].exists())
            self.assertTrue(fx["target"].is_symlink())

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

    def test_preswap_candidate_and_staged_duplicate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._cutover(cutover, fx)
            staged = fx["transaction"] / "staged-candidate"
            shutil.copytree(fx["target"], staged, copy_function=shutil.copy2)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["schema"] = 5
            metadata["phase"] = "PRE_SWAP"
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            with self.assertRaisesRegex(cutover.CutoverError, "ambiguous|staged|phase|previous.*closure"):
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
            metadata["schema"] = 5
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
            self.assertEqual(classify("enabled", False), "stale-registered")

    def test_runtime_enabled_with_exact_modern_job_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            self.assertEqual(classify("enabled", True), "healthy-registered")

    def test_runtime_classifier_rejects_non_boolean_launchd_presence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, _fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            with self.assertRaisesRegex(cutover.CutoverError, "presence evidence"):
                classify("enabled", "gui/501/foreign")

    def test_modern_bundleprogram_service_without_textual_program_can_be_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "enabled"
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            self._set_launch_service_without_program(fx, service, True)
            snapshot = cutover._modern_ownership_snapshot_from_state(
                fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                runtime_legacy_program=fx["target"] / "Contents/Resources/runtime/start.sh",
            )
            runtime = snapshot["runtime"]
            self.assertEqual(runtime["classification"], "healthy-registered")
            self.assertTrue(runtime["launchd_present"])
            self.assertEqual(runtime["loaded_program"], "")
            self.assertEqual(runtime["bundle_program"], "Contents/MacOS/AgentRuntimeRuntimeService")

    def test_legacy_exact_program_verifier_rejects_service_without_program_field(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            service = f"gui/501/{LEGACY_RUNTIME_LABEL}"
            self._set_launch_service_without_program(fx, service, True)
            with self.assertRaisesRegex(cutover.CutoverError, "program is unavailable"):
                cutover._loaded_service_program(fx["launchctl"], service)

    def test_modern_service_header_must_match_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "enabled"
            expected = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            original = cutover._service_print
            def wrong_header(launchctl, service):
                if service.endswith('/' + MODERN_RUNTIME_LABEL):
                    return subprocess.CompletedProcess(
                        [str(launchctl), 'print', service], 0,
                        stdout=f"gui/501/{LEGACY_RUNTIME_LABEL} = {{\n\tprogram = {expected}\n}}\n", stderr=""
                    )
                return original(launchctl, service)
            cutover._service_print = wrong_header
            with self.assertRaisesRegex(cutover.CutoverError, "label|target"):
                cutover._modern_ownership_snapshot_from_state(
                    fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                    runtime_legacy_program=fx["target"] / "Contents/Resources/runtime/start.sh",
                )

    def test_modern_enabled_service_present_wrong_plist_label_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "enabled"
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            self._set_launch_service_without_program(fx, service, True)
            plist = fx["target"] / "Contents/Library/LaunchAgents" / f"{MODERN_RUNTIME_LABEL}.plist"
            payload = plistlib.loads(plist.read_bytes())
            payload["Label"] = LEGACY_RUNTIME_LABEL
            plist.write_bytes(plistlib.dumps(payload))
            with self.assertRaisesRegex(cutover.CutoverError, "Label"):
                cutover._modern_ownership_snapshot_from_state(
                    fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                    runtime_legacy_program=None,
                )

    def test_modern_enabled_service_present_wrong_bundleprogram_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "enabled"
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            self._set_launch_program(fx, service, fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService")
            plist = fx["target"] / "Contents/Library/LaunchAgents" / f"{MODERN_RUNTIME_LABEL}.plist"
            payload = plistlib.loads(plist.read_bytes())
            payload["BundleProgram"] = "Contents/MacOS/ForeignHelper"
            plist.write_bytes(plistlib.dumps(payload))
            with self.assertRaisesRegex(cutover.CutoverError, "BundleProgram"):
                cutover._modern_ownership_snapshot_from_state(
                    fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                    runtime_legacy_program=None,
                )

    def test_modern_enabled_service_present_missing_helper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "enabled"
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            helper = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            self._set_launch_program(fx, service, helper)
            helper.unlink()
            with self.assertRaisesRegex(cutover.CutoverError, "helper.*missing|helper.*unsafe"):
                cutover._modern_ownership_snapshot_from_state(
                    fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                    runtime_legacy_program=None,
                )

    def test_modern_enabled_service_present_symlink_helper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "enabled"
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            helper = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            foreign = fx["root"] / "foreign-helper"
            foreign.write_text("foreign\n")
            helper.unlink()
            helper.symlink_to(foreign)
            self._set_launch_program(fx, service, helper)
            with self.assertRaisesRegex(cutover.CutoverError, "helper.*missing|helper.*unsafe"):
                cutover._modern_ownership_snapshot_from_state(
                    fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                    runtime_legacy_program=None,
                )

    def test_absent_registration_with_unexpected_modern_service_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"]["runtime_agent"] = "not-registered"
            service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            self._set_launch_service_without_program(fx, service, True)
            with self.assertRaisesRegex(cutover.CutoverError, "without registered ServiceManagement ownership"):
                cutover._modern_ownership_snapshot_from_state(
                    fx["target"], dict(fx["modern_state"]), launchctl=fx["launchctl"], uid=501,
                    runtime_legacy_program=None,
                )

    def test_schema5_cutover_accepts_bundleprogram_job_without_textual_program(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            original = cutover._service_management
            def service_management(app: Path, operation: str) -> dict[str, str]:
                state = original(app, operation)
                if operation == "register-runtime":
                    self._set_launch_service_without_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", True)
                return state
            cutover._service_management = service_management
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            runtime = json.loads((fx["transaction"] / "metadata.json").read_text())["modern_ownership_after"]["runtime"]
            self.assertTrue(runtime["launchd_present"])
            self.assertEqual(runtime["loaded_program"], "")

    def test_register_runtime_requires_approval_reaches_pending_with_created_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"].update(main_app="not-registered", runtime_agent="not-registered")
            service_operations: list[str] = []

            def service_management(app: Path, operation: str) -> dict[str, str]:
                service_operations.append(operation)
                if operation == "register-main":
                    fx["modern_state"]["main_app"] = "enabled"
                elif operation == "register-runtime":
                    fx["modern_state"]["runtime_agent"] = "requires-approval"
                    fx["set_modern_runtime_loaded"](app, False)
                elif operation == "unregister-main":
                    fx["modern_state"]["main_app"] = "not-registered"
                elif operation == "unregister-runtime":
                    fx["modern_state"]["runtime_agent"] = "not-registered"
                    fx["set_modern_runtime_loaded"](app, False)
                elif operation != "status":
                    raise AssertionError(operation)
                return dict(fx["modern_state"])

            cutover._service_management = service_management
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertTrue(metadata["operations"]["runtime_registered"])
            self.assertEqual(metadata["modern_ownership_after"]["runtime"]["registration_state"], "requires-approval")
            self.assertEqual(metadata["modern_ownership_after"]["runtime"]["classification"], "awaiting-approval")
            self.assertFalse(metadata["modern_ownership_after"]["runtime"]["loaded"])
            self.assertIn("register-runtime", service_operations)

    def test_service_management_exit_three_is_typed_without_parsing_prose(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cutover = load_module(CUTOVER_PATH, "candidate_cutover_cli_contract")
            app = Path(raw) / "Agent Runtime.app"
            executable = app / "Contents/MacOS/AgentRuntimeMenuBar"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            cutover._run = lambda argv: subprocess.CompletedProcess(
                argv,
                3,
                stdout=json.dumps({
                    "main_app": "enabled",
                    "runtime_agent": "not-registered",
                    "operation": "register-runtime",
                    "outcome": "approval-required",
                }) + "\n",
                stderr="Operation not permitted",
            )
            with self.assertRaises(Exception) as raised:
                cutover._service_management(app, "register-runtime")
            error_type = getattr(cutover, "ServiceManagementApprovalRequired", None)
            self.assertIsNotNone(error_type)
            self.assertIsInstance(raised.exception, error_type)
            self.assertEqual(raised.exception.operation, "register-runtime")
            self.assertEqual(raised.exception.state["runtime_agent"], "not-registered")

    def test_partial_pinned_authority_fails_before_product_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            target_before = provenance.candidate_closure(fx["target"])
            pins = self._pins(fx)
            with self.assertRaisesRegex(cutover.CutoverError, "requires both"):
                self._cutover(
                    cutover,
                    fx,
                    expected_candidate_sha256=pins["expected_candidate_sha256"],
                )
            self.assertEqual(provenance.candidate_closure(fx["target"]), target_before)
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(fx["service_operations"], [])

    def test_pinned_cutover_persists_authority_and_never_invokes_strict_trust(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            pins = self._pins(fx)
            strict = mock.Mock(side_effect=AssertionError("strict trust must not run"))
            cutover.provenance._verify_codesign = strict
            result = self._cutover(cutover, fx, **pins)
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(
                metadata["candidate_validation"],
                {
                    "mode": "pinned",
                    "expected_candidate_sha256": pins["expected_candidate_sha256"],
                    "expected_handoff_sha256": pins["expected_handoff_sha256"],
                },
            )
            strict.assert_not_called()

    def test_pinned_resume_reuses_persisted_handoff_hash_and_fails_on_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            pins = self._pins(fx)
            self._awaiting_approval(cutover, fx, **pins)
            tx_handoff = fx["transaction"] / "candidate-handoff.json"
            tx_handoff.write_bytes(tx_handoff.read_bytes() + b" ")
            fx["modern_state"]["runtime_agent"] = "enabled"
            fx["set_modern_runtime_loaded"](fx["target"], True)
            with self.assertRaisesRegex(Exception, "handoff SHA-256"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].is_dir())

    def test_pinned_commit_reuses_persisted_handoff_hash_and_rejects_malformed_pin_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            pins = self._pins(fx)
            self._cutover(cutover, fx, **pins)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            malformed = json.loads(json.dumps(metadata))
            del malformed["candidate_validation"]["expected_handoff_sha256"]
            metadata_path.write_text(json.dumps(malformed, indent=2) + "\n")
            with self.assertRaisesRegex(cutover.CutoverError, "validation metadata"):
                cutover.commit_transaction(fx["transaction"], fx["target"])

            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            tx_handoff = fx["transaction"] / "candidate-handoff.json"
            tx_handoff.write_bytes(tx_handoff.read_bytes() + b" ")
            with self.assertRaisesRegex(Exception, "handoff SHA-256"):
                cutover.commit_transaction(fx["transaction"], fx["target"])
            self.assertTrue(fx["transaction"].is_dir())

    def test_approval_denial_reaches_awaiting_approval_without_claiming_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            result = self._awaiting_approval(cutover, fx)
            self.assertEqual(result["status"], "AWAITING_APPROVAL")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["schema"], 5)
            self.assertEqual(metadata["modern_runtime_label"], MODERN_RUNTIME_LABEL)
            self.assertEqual(metadata["status"], "AWAITING_APPROVAL")
            self.assertEqual(metadata["phase"], "APP_SWAPPED")
            self.assertFalse(metadata["operations"]["runtime_registered"])
            self.assertTrue(metadata["operations"]["runtime_approval_requested"])
            self.assertEqual(metadata["modern_registration"]["runtime_agent"], "not-registered")
            self.assertEqual(metadata["modern_ownership_after"]["runtime"]["classification"], "absent")
            self.assertFalse(metadata["modern_ownership_after"]["runtime"]["loaded"])
            provenance.validate_candidate(fx["target"], fx["transaction"] / "candidate-handoff.json")
            self.assertTrue((fx["transaction"] / "previous-app").is_dir())
            self.assertFalse(fx["ui_plist"].exists())
            self.assertFalse(fx["runtime_plist"].exists())

    def test_future_transaction_records_schema5_modern_label_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["schema"], 5)
            self.assertEqual(metadata["modern_runtime_label"], MODERN_RUNTIME_LABEL)

    def test_commit_rejects_awaiting_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            with self.assertRaisesRegex(cutover.CutoverError, "pending"):
                cutover.commit_transaction(fx["transaction"], fx["target"])
            self.assertTrue(fx["transaction"].is_dir())

    def test_resume_enabled_exact_helper_transitions_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["modern_state"]["runtime_agent"] = "enabled"
            fx["set_modern_runtime_loaded"](fx["target"], True)
            result = cutover.resume_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "PENDING")
            self.assertTrue(metadata["operations"]["runtime_registered"])

    def test_resume_enabled_bundleprogram_job_without_textual_program_transitions_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["modern_state"]["runtime_agent"] = "enabled"
            self._set_launch_service_without_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", True)
            result = cutover.resume_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "PENDING")
            runtime = json.loads((fx["transaction"] / "metadata.json").read_text())["modern_ownership_after"]["runtime"]
            self.assertTrue(runtime["launchd_present"])
            self.assertEqual(runtime["loaded_program"], "")

    def test_resume_requires_approval_stays_awaiting_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["modern_state"]["runtime_agent"] = "requires-approval"
            fx["service_operations"].clear()
            result = cutover.resume_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "AWAITING_APPROVAL")
            self.assertEqual(result["outcome"], "OPERATOR_INPUT_REQUIRED")
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertNotIn("register-runtime", operations)
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertTrue(metadata["operations"]["runtime_registered"])

    def test_resume_absent_retries_once_and_typed_denial_stays_awaiting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["service_operations"].clear()
            result = cutover.resume_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "AWAITING_APPROVAL")
            self.assertEqual(result["outcome"], "OPERATOR_INPUT_REQUIRED")
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertEqual(operations.count("register-runtime"), 1)
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertFalse(metadata["operations"]["runtime_registered"])
            self.assertTrue(metadata["operations"]["runtime_approval_requested"])

    def test_resume_absent_successful_registration_exact_helper_transitions_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["service_operations"].clear()

            def service_management(app: Path, operation: str) -> dict[str, str]:
                fx["service_operations"].append((str(app), operation))
                if operation == "register-runtime":
                    fx["modern_state"]["runtime_agent"] = "enabled"
                    fx["set_modern_runtime_loaded"](app, True)
                elif operation != "status":
                    raise AssertionError(operation)
                return dict(fx["modern_state"])

            cutover._service_management = service_management
            result = cutover.resume_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "PENDING")
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertEqual(operations.count("register-runtime"), 1)

    def test_resume_mutated_helper_fails_candidate_provenance_and_retains_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["modern_state"]["runtime_agent"] = "enabled"
            self._set_launch_service_without_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", True)
            helper = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"tampered-after-checkpoint\n")
            with self.assertRaisesRegex(Exception, "candidate identity"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "AWAITING_APPROVAL")

    def test_resume_legacy_ownership_reappeared_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            shutil.copy2(fx["transaction"] / "previous-ui.plist", fx["ui_plist"])
            with self.assertRaisesRegex(cutover.CutoverError, "legacy"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].is_dir())

    def test_resume_config_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["runtime_env"].write_text("CONTROL_PLANE_API_KEY=changed-secret\n")
            fx["runtime_env"].chmod(0o600)
            with self.assertRaisesRegex(cutover.CutoverError, "config"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].is_dir())

    def test_resume_desired_state_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["desired"].unlink()
            with self.assertRaisesRegex(cutover.CutoverError, "desired-state"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].is_dir())

    def test_resume_fails_closed_for_no_transaction_pending_partial_and_unsupported_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            resume = getattr(cutover, "resume_transaction", None)
            self.assertIsNotNone(resume, "resume transaction surface is required")
            with self.assertRaises(cutover.CutoverError):
                resume(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)

            self._awaiting_approval(cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            original = json.loads(metadata_path.read_text())
            for status, schema in (("PENDING", 4), ("PARTIAL", 4), ("AWAITING_APPROVAL", 999)):
                with self.subTest(status=status, schema=schema):
                    value = dict(original)
                    value["status"] = status
                    value["schema"] = schema
                    metadata_path.write_text(json.dumps(value, indent=2) + "\n")
                    with self.assertRaises(cutover.CutoverError):
                        resume(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501)
            metadata_path.write_text(json.dumps(original, indent=2) + "\n")

    def test_resume_path_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            metadata_path = fx["transaction"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["paths"]["target_app"] = str(fx["root"] / "wrong.app")
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            with self.assertRaisesRegex(cutover.CutoverError, "path|target"):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )

    def test_resume_candidate_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            executable = fx["target"] / "Contents/MacOS/AgentRuntimeMenuBar"
            executable.write_bytes(executable.read_bytes() + b"tampered\n")
            with self.assertRaises(Exception):
                cutover.resume_transaction(
                    fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
                )
            self.assertTrue(fx["transaction"].is_dir())

    def test_rollback_from_awaiting_approval_restores_exact_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            previous_closure = provenance.candidate_closure(fx["target"])
            self._awaiting_approval(cutover, fx)
            cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(provenance.candidate_closure(fx["target"]), previous_closure)
            self.assertEqual(fx["ui_plist"].read_bytes(), fx["ui_before"])
            self.assertEqual(fx["runtime_plist"].read_bytes(), fx["runtime_before"])

    def test_rollback_approval_request_does_not_invent_runtime_unregister_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["service_operations"].clear()
            cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertNotIn("unregister-runtime", operations)

    def test_rollback_awaiting_approval_compensates_runtime_only_after_registration_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            self._awaiting_approval(cutover, fx)
            fx["modern_state"]["runtime_agent"] = "requires-approval"
            fx["service_operations"].clear()
            cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertIn("unregister-runtime", operations)

    def test_arbitrary_runtime_registration_error_still_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(raw)
            previous_closure = provenance.candidate_closure(fx["target"])
            original = cutover._service_management

            def service_management(app: Path, operation: str) -> dict[str, str]:
                if operation == "register-runtime":
                    raise cutover.CutoverError("arbitrary registration failure")
                return original(app, operation)

            cutover._service_management = service_management
            with self.assertRaisesRegex(cutover.CutoverError, "rollback restored"):
                self._cutover(cutover, fx)
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(provenance.candidate_closure(fx["target"]), previous_closure)

    def test_requires_approval_is_distinct_from_enabled_health(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            classify = getattr(cutover, "_classify_runtime_ownership", None)
            self.assertIsNotNone(classify, "runtime ownership classification is required")
            self.assertEqual(classify("requires-approval", False), "awaiting-approval")

    def test_changed_helper_generation_forces_runtime_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(
                raw, predecessor_revision="7077257837bdaf76ef1558fe78b900ec3af68788"
            )
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                f"gui/501/{MODERN_RUNTIME_LABEL}",
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

    def test_split_v1_refresh_waits_for_launchd_absence_before_swap_and_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            predecessor_revision = "99518e694467be8525d813a4e638c4c8e0324365"
            provenance, cutover, fx = self._fixture(raw, predecessor_revision=predecessor_revision)
            modern_service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                modern_service,
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"changed-generation-delayed-unload\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])

            original_service_management = cutover._service_management
            original_service_loaded = cutover._service_loaded
            original_ownership_snapshot = cutover._modern_ownership_snapshot
            events: list[str] = []
            polls = 0

            def installed_revision() -> str:
                manifest = json.loads(
                    (fx["target"] / "Contents/Resources/runtime-manifest.json").read_text()
                )
                return str(manifest["runtime_revision"])

            def service_management(app: Path, operation: str) -> dict[str, str]:
                if operation == "unregister-runtime":
                    fx["service_operations"].append((str(app), operation))
                    fx["modern_state"]["runtime_agent"] = "not-registered"
                    events.append("unregister-returned")
                    return dict(fx["modern_state"])
                if operation == "register-runtime":
                    loaded = set(json.loads(fx["launch_state"].read_text()))
                    self.assertNotIn(modern_service, loaded)
                    events.append("register-runtime")
                return original_service_management(app, operation)

            def service_loaded(launchctl: Path, service: str) -> bool:
                nonlocal polls
                if (
                    service == modern_service
                    and "unregister-returned" in events
                    and "register-runtime" not in events
                ):
                    polls += 1
                    self.assertEqual(installed_revision(), predecessor_revision)
                    if polls < 3:
                        events.append("launchd-still-loaded")
                        return True
                    fx["set_modern_runtime_loaded"](fx["target"], False)
                    events.append("launchd-absent")
                    return False
                return original_service_loaded(launchctl, service)

            def ownership_snapshot(app: Path, **kwargs):
                if "unregister-returned" in events:
                    self.assertIn("launchd-absent", events)
                    events.append("candidate-inspection")
                return original_ownership_snapshot(app, **kwargs)

            cutover._service_management = service_management
            cutover._service_loaded = service_loaded
            cutover._modern_ownership_snapshot = ownership_snapshot
            cutover.time.sleep = lambda _seconds: None

            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            self.assertGreaterEqual(polls, 3)
            self.assertLess(events.index("launchd-absent"), events.index("candidate-inspection"))
            self.assertLess(events.index("launchd-absent"), events.index("register-runtime"))

    def test_split_v1_unregister_crash_after_consequence_restores_predecessor(self) -> None:
        class ProcessDeath(BaseException):
            pass

        with tempfile.TemporaryDirectory() as raw:
            predecessor_revision = "99518e694467be8525d813a4e638c4c8e0324365"
            provenance, cutover, fx = self._fixture(raw, predecessor_revision=predecessor_revision)
            modern_service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                modern_service,
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"crash-after-unregister-consequence\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])
            original_service_management = cutover._service_management

            def service_management(app: Path, operation: str) -> dict[str, str]:
                result = original_service_management(app, operation)
                if operation == "unregister-runtime":
                    raise ProcessDeath()
                return result

            cutover._service_management = service_management
            with self.assertRaises(ProcessDeath):
                self._cutover(cutover, fx)

            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["phase"], "PRE_SWAP")
            self.assertEqual(metadata["predecessor_runtime_unregister"], "in-flight")
            self.assertFalse(metadata["operations"]["runtime_unregistered"])
            self.assertIn(fx["modern_state"]["runtime_agent"], {"not-found", "not-registered"})
            self.assertNotIn(modern_service, set(json.loads(fx["launch_state"].read_text())))

            fx["service_operations"].clear()
            result = cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(fx["modern_state"]["runtime_agent"], "enabled")
            self.assertIn(modern_service, set(json.loads(fx["launch_state"].read_text())))
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertIn("register-runtime", operations)

    def test_split_v1_unregister_crash_before_consequence_does_not_invent_restore(self) -> None:
        class ProcessDeath(BaseException):
            pass

        with tempfile.TemporaryDirectory() as raw:
            predecessor_revision = "99518e694467be8525d813a4e638c4c8e0324365"
            provenance, cutover, fx = self._fixture(raw, predecessor_revision=predecessor_revision)
            modern_service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                modern_service,
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"crash-before-unregister-consequence\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])
            original_service_management = cutover._service_management

            def service_management(app: Path, operation: str) -> dict[str, str]:
                if operation == "unregister-runtime":
                    raise ProcessDeath()
                return original_service_management(app, operation)

            cutover._service_management = service_management
            with self.assertRaises(ProcessDeath):
                self._cutover(cutover, fx)

            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["predecessor_runtime_unregister"], "in-flight")
            self.assertFalse(metadata["operations"]["runtime_unregistered"])
            self.assertEqual(fx["modern_state"]["runtime_agent"], "enabled")
            self.assertIn(modern_service, set(json.loads(fx["launch_state"].read_text())))

            fx["service_operations"].clear()
            result = cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertFalse(fx["transaction"].exists())
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertNotIn("register-runtime", operations)
            self.assertNotIn("unregister-runtime", operations)
            self.assertEqual(fx["modern_state"]["runtime_agent"], "enabled")
            self.assertIn(modern_service, set(json.loads(fx["launch_state"].read_text())))

    def test_split_v1_unregister_crash_after_completion_before_swap_restores_predecessor(self) -> None:
        class ProcessDeath(BaseException):
            pass

        with tempfile.TemporaryDirectory() as raw:
            predecessor_revision = "99518e694467be8525d813a4e638c4c8e0324365"
            provenance, cutover, fx = self._fixture(raw, predecessor_revision=predecessor_revision)
            modern_service = f"gui/501/{MODERN_RUNTIME_LABEL}"
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                modern_service,
                fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService",
            )
            helper = fx["candidate"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_bytes(helper.read_bytes() + b"crash-after-unregister-complete\n")
            provenance.seal_candidate(fx["candidate"], fx["handoff"])
            original_wait = cutover._wait_for_service_absence

            def wait_then_crash(*args, **kwargs) -> None:
                original_wait(*args, **kwargs)
                raise ProcessDeath()

            cutover._wait_for_service_absence = wait_then_crash
            with self.assertRaises(ProcessDeath):
                self._cutover(cutover, fx)

            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["phase"], "PRE_SWAP")
            self.assertEqual(metadata["predecessor_runtime_unregister"], "observed-complete")
            self.assertTrue(metadata["operations"]["runtime_unregistered"])
            self.assertIn(fx["modern_state"]["runtime_agent"], {"not-found", "not-registered"})
            self.assertNotIn(modern_service, set(json.loads(fx["launch_state"].read_text())))

            fx["service_operations"].clear()
            result = cutover.rollback_transaction(
                fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=501
            )
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertFalse(fx["transaction"].exists())
            self.assertEqual(fx["modern_state"]["runtime_agent"], "enabled")
            self.assertIn(modern_service, set(json.loads(fx["launch_state"].read_text())))
            operations = [operation for _, operation in fx["service_operations"]]
            self.assertIn("register-runtime", operations)

    def test_unchanged_healthy_runtime_is_not_destructively_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            fx["modern_state"].update(main_app="enabled", runtime_agent="enabled")
            self._set_launch_program(
                fx,
                f"gui/501/{MODERN_RUNTIME_LABEL}",
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

    def test_new_cutover_records_schema5_pre_swap_ownership_and_operation_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _, cutover, fx = self._fixture(raw)
            result = self._cutover(cutover, fx)
            self.assertEqual(result["status"], "PENDING")
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["schema"], 5)
            self.assertEqual(metadata["modern_runtime_label"], MODERN_RUNTIME_LABEL)
            self.assertEqual(metadata["phase"], "APP_SWAPPED")
            self.assertIn("modern_ownership_before", metadata)
            self.assertIn("runtime", metadata["modern_ownership_before"])
            runtime = metadata["modern_ownership_before"]["runtime"]
            self.assertIn(runtime["classification"], {"healthy-registered", "stale-registered", "absent", "awaiting-approval"})
            self.assertIn("helper_sha256", runtime)
            self.assertIn("plist_sha256", runtime)
            self.assertEqual(
                set(metadata["operations"]),
                {"main_registered", "main_unregistered", "runtime_unregistered", "runtime_registered", "runtime_approval_requested"},
            )

    def test_future_rollback_does_not_resurrect_stale_preexisting_runtime_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            provenance, cutover, fx = self._fixture(
                raw, predecessor_revision="7077257837bdaf76ef1558fe78b900ec3af68788"
            )
            fx["modern_state"].update(main_app="not-found", runtime_agent="enabled")
            self._set_launch_program(fx, f"gui/501/{MODERN_RUNTIME_LABEL}", None)
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
                "runtime_approval_requested": False,
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
        self.assertNotIn("tunnel-client", branch)

    def test_prebuilt_install_contract_keeps_strict_form_and_requires_exact_pinned_pair(self) -> None:
        text = (ROOT / "install.sh").read_text()
        branch_start = text.index("--install-prebuilt)")
        branch_end = text.index("--resume-cutover)", branch_start)
        branch = text[branch_start:branch_end]
        self.assertIn('[[ "$#" == "3" ]]', branch)
        self.assertIn('[[ "$#" == "7"', branch)
        self.assertIn('"$4" == "--expected-candidate-sha256"', branch)
        self.assertIn('"$6" == "--expected-handoff-sha256"', branch)
        self.assertIn('is_lower_sha256 "$5"', branch)
        self.assertIn('is_lower_sha256 "$7"', branch)
        self.assertIn('--expected-candidate-sha256 "$5"', branch)
        self.assertIn('--expected-handoff-sha256 "$7"', branch)
        self.assertNotIn("PINNED_ARGS", branch)
        self.assertNotIn("--skip-trust", branch)
        self.assertNotIn("--no-verify", branch)

    def test_package_script_seals_external_candidate_only_after_final_integrity_checks(self) -> None:
        text = (ROOT / "macos" / "package_app.sh").read_text()
        sign = text.index('/usr/bin/codesign --force --deep --sign "$SIGNING_IDENTITY" "$APP"')
        verify = text.index('/usr/bin/codesign --verify --deep --strict "$APP"', sign)
        final_manifest = text.index('package_provenance.py" validate', verify)
        seal = text.index('package_provenance.py" seal', final_manifest)
        self.assertLess(sign, verify)
        self.assertLess(verify, final_manifest)
        self.assertIn('package_provenance.py" publish', text)
        publish = text.index('package_provenance.py" publish', seal)
        self.assertLess(final_manifest, seal)
        self.assertLess(seal, publish)
        self.assertIn('STAGED_PUBLICATION="$TEMP_ROOT/candidate"', text)
        self.assertNotIn('CANDIDATE_HANDOFF="$REPO_ROOT/build/Agent Runtime.candidate.json"', text)
        self.assertNotIn('APP="$REPO_ROOT/build/Agent Runtime.app"', text)

    def test_default_install_composes_build_and_prebuilt_cutover_but_leaves_transaction_pending(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn('PACKAGE_OUTPUT="$("$ROOT/macos/package_app.sh")"', text)
        build = text.index('PACKAGE_OUTPUT="$("$ROOT/macos/package_app.sh")"')
        source_app = text.index('candidate_app=', build)
        source_handoff = text.index('candidate_handoff=', source_app)
        prebuilt = text.index('"$ROOT/install.sh" --install-prebuilt "$SOURCE_APP" "$CANDIDATE_HANDOFF"', source_handoff)
        self.assertLess(build, source_app)
        self.assertLess(source_app, source_handoff)
        self.assertLess(source_handoff, prebuilt)
        self.assertNotIn('"$ROOT/install.sh" --commit-cutover', text[prebuilt:])
        self.assertIn('pending explicit commit', text.lower())
        self.assertIn('--commit-cutover', text)
        self.assertIn('--rollback-cutover', text)
        self.assertNotIn('validate_package() {', text)
        self.assertNotIn('STAGING_APP="$TARGET_APPS/.Agent Runtime.app.', text)
        self.assertNotIn('/usr/bin/codesign --verify --deep --strict "$app"', text)



if __name__ == "__main__":
    unittest.main()
