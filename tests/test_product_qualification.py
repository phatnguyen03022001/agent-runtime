"""TASK-0200 synthetic consumer qualification; no live state or control plane.

Run with unittest --failfast. Stop at the first unmet product predicate;
later qualification rows must not be represented as passing by implication.
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests import test_runtime_config as config_owner
from tests import test_candidate_cutover as cutover_owner
from tests import test_supervised_lifecycle as lifecycle_owner

ROOT = Path(__file__).resolve().parents[1]


class ProductQualificationTests(unittest.TestCase):
    def _admission_case(
        self,
        *,
        existing: bool,
        expected_reason: str,
        output: str | None = None,
        client_exit: int = 1,
        malformed_utf8: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="task0200-") as raw:
            root = Path(raw).resolve()
            home = root / "home"
            workspace = root / "workspace"
            tools = root / "tools"
            for directory in (home, workspace, tools):
                directory.mkdir()
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            values = {
                "CONTROL_PLANE_API_KEY": "TASK0200_SUBMITTED_SYNTHETIC_SECRET",
                "CONTROL_PLANE_TUNNEL_ID": config_owner.RuntimeConfigTests.TUNNEL_ID,
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "TASK0200 Synthetic Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "task0200@example.invalid",
            }
            before = None
            identity = None
            if existing:
                canonical.parent.mkdir(parents=True)
                predecessor = {
                    **values,
                    "CONTROL_PLANE_API_KEY": "TASK0200_PREDECESSOR_SYNTHETIC_SECRET",
                    "AGENT_RUNTIME_GIT_NAME": "TASK0200 Predecessor",
                }
                canonical.write_bytes(config_owner.runtime_config._prebuilt_payload(predecessor))
                canonical.chmod(0o600)
                before = canonical.read_bytes()
                info = canonical.stat()
                identity = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))

            argv_capture = root / "tunnel-argv.txt"
            if output is not None or malformed_utf8:
                owner = config_owner.RuntimeConfigTests()
                client = owner._write_tunnel_client(root, output=output or "", exit_code=client_exit)
                script = client.read_text()
                script = script.replace(
                    "#!/bin/sh\n",
                    "#!/bin/sh\n"
                    + "printf '%s\\n' \"$@\" > " + shlex.quote(str(argv_capture)) + "\n",
                    1,
                )
                if malformed_utf8:
                    # Invalid UTF-8 from the synthetic tunnel response, with no network.
                    script = script[:script.index("for arg")] + "printf '\\377'\nexit 0\n"
                client.write_text(script)

            env = {
                "HOME": str(home),
                "PATH": str(tools),
                "TMPDIR": str(root),
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            command = [
                sys.executable,
                str(ROOT / "macos/runtime_config.py"),
                "--reconfigure-stdin" if existing else "--prebuilt-stdin",
                str(canonical),
            ]
            result = subprocess.run(
                command,
                input=json.dumps(values),
                env=env,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            # Evaluate persistence and secret safety even when the error mapping fails.
            if existing:
                self.assertEqual(canonical.read_bytes(), before)
                info = canonical.stat()
                self.assertEqual((info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)), identity)
            else:
                self.assertFalse(canonical.exists())
            observed = result.stdout + result.stderr + "\n".join(command)
            if argv_capture.exists():
                observed += argv_capture.read_text()
            self.assertNotIn(values["CONTROL_PLANE_API_KEY"], observed)
            self.assertLess(len((result.stdout + result.stderr).encode()), 65536)
            self.assertNotEqual(result.returncode, 0)
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError:
                self.fail(
                    f"Expected {expected_reason} structured admission error; "
                    f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
                )
            self.assertEqual(response["reason_code"], expected_reason)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")

    def test_q01_missing_tunnel_client_preserves_absent_and_existing_canonical(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                self._admission_case(existing=existing, expected_reason="TUNNEL_CLIENT_UNAVAILABLE")

    def test_q02_401_preserves_config_and_keeps_submitted_secret_out_of_argv_and_output(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                self._admission_case(
                    existing=existing,
                    expected_reason="INVALID_CREDENTIAL",
                    output=json.dumps({"error": {"status": 401, "message": "TASK0200_SUBMITTED_SYNTHETIC_SECRET"}}),
                )

    def test_q03_403_and_404_preserve_config(self) -> None:
        for status, reason in ((403, "TUNNEL_ACCESS_DENIED"), (404, "TUNNEL_NOT_FOUND")):
            for existing in (False, True):
                with self.subTest(status=status, existing=existing):
                    self._admission_case(
                        existing=existing,
                        expected_reason=reason,
                        output=json.dumps({"error": {"status": status}}),
                    )

    def test_q04_transient_and_malformed_json_preserve_config(self) -> None:
        for output, code in ((json.dumps({"error": {"status": 503}}), 1), ("not-json", 0)):
            for existing in (False, True):
                with self.subTest(output=output, existing=existing):
                    self._admission_case(
                        existing=existing,
                        expected_reason="CONTROL_PLANE_UNAVAILABLE",
                        output=output,
                        client_exit=code,
                    )

    def test_q04_undecodable_tunnel_response_maps_unavailable_and_preserves_config(self) -> None:
        self._admission_case(
            existing=True,
            expected_reason="CONTROL_PLANE_UNAVAILABLE",
            malformed_utf8=True,
        )

    def test_q05_removed_configured_workspace_fails_without_consumer_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="task0200-workspace-") as raw:
            root = Path(raw).resolve()
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir()
            state = home / "Library/Application Support/Agent Runtime"
            state.mkdir(parents=True)
            canonical = state / "runtime.env"
            values = {
                "CONTROL_PLANE_API_KEY": "Q05_SYNTHETIC_SECRET",
                "CONTROL_PLANE_TUNNEL_ID": config_owner.RuntimeConfigTests.TUNNEL_ID,
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Q05 Synthetic Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "q05@example.invalid",
            }
            canonical.write_bytes(config_owner.runtime_config._prebuilt_payload(values))
            canonical.chmod(0o600)
            (state / "protected-runtime-running").touch()
            (state / "current-payload").write_text("a" * 64 + "\n")
            before = {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}
            info = canonical.stat()
            identity = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
            workspace.rmdir()
            client = config_owner.RuntimeConfigTests()._write_tunnel_client(root, output="{}", exit_code=0)
            client.write_text("#!/bin/sh\n: > " + shlex.quote(str(root / "client-called")) + "\nexit 99\n")
            env = {"HOME": str(home), "PATH": str(client.parent), "TMPDIR": str(root),
                   "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}
            results = []
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "macos/runtime_config.py"), "--reconfigure-stdin", str(canonical)],
                    input=json.dumps(values), env=env, cwd=root, capture_output=True,
                    text=True, check=False, timeout=20,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["reason_code"], "WORKSPACE_UNAVAILABLE")
                self.assertEqual(result.stderr, "")
                self.assertNotIn(values["CONTROL_PLANE_API_KEY"], result.stdout)
                results.append(result.stdout)
            self.assertEqual(results[0], results[1])
            self.assertFalse((root / "client-called").exists())
            self.assertEqual({path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}, before)
            info = canonical.stat()
            self.assertEqual((info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)), identity)

    def test_q06_foreign_and_ambiguous_port_ownership_grants_no_signal_or_rebind_authority(self) -> None:
        for pids in ("777", "777\n778"):
            with self.subTest(pids=pids):
                owner = lifecycle_owner.SupervisedLifecycleTests()
                owner.setUp()
                try:
                    # All process observations are synthetic, including the second PID.
                    ps = owner.bin / "ps"
                    ps.write_text("#!/bin/sh\nprintf '%s\\n' '/usr/bin/python3 -m http.server 8080'\n")
                    launchctl = owner.bin / "launchctl"
                    original = launchctl.read_text()
                    launchctl.write_text(original.replace(
                        "case \"$1\" in\n",
                        "printf '%s\\n' \"$1\" >> \"$HOME/fake-launchd/actions.log\"\ncase \"$1\" in\n", 1,
                    ))
                    env = {"FAKE_FOREIGN_PORT_PID": pids}
                    status = owner.run_status_json(extra_env=env)
                    self.assertEqual(status["state"], "attention")
                    self.assertEqual(status["control"], "none")
                    for action in ("start", "stop", "restart"):
                        result = owner.run_start(action, extra_env=env)
                        self.assertNotEqual(result.returncode, 0)
                    log = owner.state / "actions.log"
                    if log.exists():
                        self.assertTrue(set(log.read_text().splitlines()) <= {"print"})
                    self.assertFalse((owner.state / "starts.log").exists())
                    self.assertFalse((owner.state / "runtime.pid").exists())
                    self.assertFalse((owner.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())
                finally:
                    owner.tearDown()

    def _zero_cost_fixture(self, raw: str):
        owner = cutover_owner.CandidateCutoverTests()
        cutover, fx = owner._zero_cost_collision_fixture(raw)
        predecessor = owner._prepare_zero_cost_persistent_current_predecessor(cutover, fx)
        # Make predecessor/candidate distinguishable without altering the sealed candidate.
        (fx["target"] / "predecessor-generation").write_text("synthetic predecessor\n")
        return cutover, fx, predecessor

    def _cutover(self, cutover, fx, **kwargs):
        return cutover.cutover_candidate(
            fx["app"], fx["handoff"], payload_release=fx["payload_release"],
            target_app=fx["target"],
            ui_plist=fx["home"] / "Library/LaunchAgents/com.picmao.agent-runtime-ui.plist",
            runtime_plist=fx["home"] / "Library/LaunchAgents/com.picmao.agent-runtime-runtime.plist",
            state_dir=fx["state_dir"], transaction_dir=fx["transaction"],
            home=fx["home"], launchctl=fx["launchctl"], uid=os.getuid(), **kwargs,
        )

    def test_q07_interrupted_zero_cost_before_and_after_swap_restores_exact_predecessor(self) -> None:
        for stage, phase in (("after_predecessor_shutdown", "PRE_SWAP"), ("after_app_swap", "APP_SWAPPED")):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix="task0200-cutover-") as raw:
                cutover, fx, predecessor = self._zero_cost_fixture(raw)
                before = cutover._rollback_app_closure(fx["target"])
                config = (fx["state_dir"] / "runtime.env").read_bytes()
                real_inject = cutover._inject

                def interrupt(failures, current):
                    if current == stage:
                        raise KeyboardInterrupt("synthetic interruption")
                    return real_inject(failures, current)

                with mock.patch.object(cutover, "_inject", side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self._cutover(cutover, fx)
                metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
                self.assertEqual(metadata["kind"], "zero-cost")
                self.assertEqual(metadata["phase"], phase)
                result = cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=os.getuid())
                self.assertEqual(result["status"], "ROLLED_BACK")
                self.assertEqual(cutover._rollback_app_closure(fx["target"]), before)
                self.assertEqual(predecessor["current_plist"].read_bytes(), predecessor["predecessor_bytes"])
                self.assertTrue(predecessor["desired"].exists())
                self.assertEqual((fx["state_dir"] / "runtime.env").read_bytes(), config)
                self.assertFalse(fx["transaction"].exists())

    def test_q07_interrupted_zero_cost_with_unverified_target_retains_evidence_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="task0200-ambiguous-cutover-") as raw:
            cutover, fx, _ = self._zero_cost_fixture(raw)
            real_inject = cutover._inject

            def interrupt(failures, stage):
                if stage == "after_app_swap":
                    raise KeyboardInterrupt("synthetic interruption")
                return real_inject(failures, stage)

            with mock.patch.object(cutover, "_inject", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self._cutover(cutover, fx)
            # The installed generation no longer matches the transaction's sealed identity.
            helper = fx["target"] / "Contents/MacOS/AgentRuntimeRuntimeService"
            helper.write_text("#!/bin/sh\n# foreign synthetic generation\nexit 0\n")
            with self.assertRaises(cutover.provenance.PackageProvenanceError):
                cutover.provenance.validate_zero_cost_candidate(
                    fx["target"], fx["handoff"], fx["payload_release"],
                )
            before = cutover._rollback_app_closure(fx["target"])
            try:
                result = cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=os.getuid())
            except cutover.CutoverError:
                self.assertTrue((fx["transaction"] / "metadata.json").is_file())
                self.assertEqual(cutover._rollback_app_closure(fx["target"]), before)
            else:
                self.fail(
                    "Unsafe interrupted zero-cost recovery accepted an unverified installed generation: "
                    f"status={result['status']}, transaction_retained={fx['transaction'].exists()}, "
                    f"target_preserved={cutover._rollback_app_closure(fx['target']) == before}"
                )

    @staticmethod
    def _config_identity(path: Path):
        info = path.stat()
        return (path.read_bytes(), info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))

    def test_q08_material_failures_restore_exact_predecessor_or_retain_partial_evidence(self) -> None:
        for stage in ("after_predecessor_shutdown", "after_app_swap", "after_launchagent_bootstrap", "after_pointer_swap"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix="task0200-rollback-") as raw:
                cutover, fx, predecessor = self._zero_cost_fixture(raw)
                before = cutover._rollback_app_closure(fx["target"])
                config = fx["state_dir"] / "runtime.env"
                config_before = self._config_identity(config)
                with self.assertRaisesRegex(cutover.CutoverError, "rollback restored previous state"):
                    self._cutover(cutover, fx, fail_stages={stage})
                self.assertEqual(cutover._rollback_app_closure(fx["target"]), before)
                self.assertEqual(predecessor["current_plist"].read_bytes(), predecessor["predecessor_bytes"])
                self.assertEqual(predecessor["pointer"].read_text(), fx["closure"] + "\n")
                self.assertTrue(predecessor["desired"].exists())
                self.assertEqual(self._config_identity(config), config_before)
                self.assertFalse(fx["transaction"].exists())
        with tempfile.TemporaryDirectory(prefix="task0200-partial-") as raw:
            cutover, fx, _ = self._zero_cost_fixture(raw)
            real_copy = cutover._copy_rollback_app

            def fail_restore(source, destination):
                if source == fx["transaction"] / "previous-app":
                    raise OSError("synthetic predecessor restoration failure")
                return real_copy(source, destination)

            with mock.patch.object(cutover, "_copy_rollback_app", side_effect=fail_restore):
                with self.assertRaisesRegex(cutover.CutoverError, "rollback incomplete"):
                    self._cutover(cutover, fx, fail_stages={"after_app_swap"})
            metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "PARTIAL")
            self.assertTrue((fx["transaction"] / "previous-app").is_dir())
            self.assertTrue((fx["transaction"] / "candidate-handoff.json").is_file())
            self.assertLess(len(metadata["last_error"].encode()), 1024)

    def test_q09_interrupted_payload_switch_selects_only_verified_release_and_recovers(self) -> None:
        for stage in ("after_payload_publish", "after_pointer_switch"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix="task0200-update-") as raw:
                cutover, fx, predecessor = self._zero_cost_fixture(raw)
                self._cutover(cutover, fx)
                cutover.commit_transaction(fx["transaction"], fx["target"])
                old_pointer = predecessor["pointer"].read_bytes()
                witnesses = cutover._payload_update_witnesses(fx["target"], fx["state_dir"], predecessor["current_plist"])
                source = Path(raw) / "update-source"
                source.mkdir()
                (source / "__init__.py").write_text("__all__ = []\n")
                (source / "server.py").write_text("VALUE = 2\n")
                substrate = cutover._current_substrate_manifest(fx["target"])
                published = cutover.provenance.publish_payload_release(
                    source, Path(raw) / "incoming-payloads", revision="1" * 40, tree="2" * 40,
                    requirements_lock_sha256=substrate["requirements_lock_sha256"],
                    python_major_minor=substrate["required_python_major_minor"],
                    public_tool_count=substrate["expected_public_tool_count"],
                    public_surface_sha256=substrate["expected_public_surface_sha256"],
                )
                incoming = Path(published["release_path"])
                real_inject = cutover._inject

                def interrupt(failures, current):
                    if current == stage:
                        raise KeyboardInterrupt("synthetic payload interruption")
                    return real_inject(failures, current)

                with (
                    mock.patch.object(cutover, "_runtime_managed_running", return_value=False),
                    mock.patch.object(cutover, "_run_runtime_lifecycle", side_effect=AssertionError("stopped fixture must not start")),
                    mock.patch.object(cutover, "_inject", side_effect=interrupt),
                ):
                    try:
                        with self.assertRaises(KeyboardInterrupt):
                            cutover.activate_payload_release(
                                incoming, target_app=fx["target"], state_dir=fx["state_dir"],
                                transaction_dir=fx["transaction"], home=fx["home"],
                                launchctl=fx["launchctl"], uid=os.getuid(),
                            )
                    except cutover.CutoverError as exc:
                        metadata_path = fx["transaction"] / "metadata.json"
                        retained = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
                        self.assertEqual(predecessor["pointer"].read_bytes(), old_pointer)
                        self.assertEqual(cutover._payload_update_witnesses(fx["target"], fx["state_dir"], predecessor["current_plist"]), witnesses)
                        staged = fx["transaction"] / "payloads" / published["content_closure"]
                        self.fail(
                            "Valid sealed payload could not reach the requested interruption: "
                            f"stage={stage}, status={retained.get('status')}, phase={retained.get('phase')}, "
                            f"transaction_retained={fx['transaction'].exists()}, metadata_retained={metadata_path.exists()}, "
                            f"staged_mode={stat.S_IMODE(staged.stat().st_mode):04o}, "
                            f"cause={type(exc.__cause__).__name__}, pointer_and_consumer_witnesses_preserved=True"
                        )
                metadata = json.loads((fx["transaction"] / "metadata.json").read_text())
                self.assertEqual(metadata["kind"], "payload-update")
                self.assertEqual(metadata["status"], "RUNNING")
                selected = cutover._current_payload_closure(predecessor["pointer"], uid=os.getuid())
                self.assertEqual(selected, published["content_closure"] if stage == "after_pointer_switch" else fx["closure"])
                cutover._validate_payload_for_substrate(fx["state_dir"] / "payloads" / selected, substrate)
                result = cutover.rollback_transaction(fx["transaction"], fx["target"], launchctl=fx["launchctl"], uid=os.getuid())
                self.assertEqual(result["status"], "ROLLED_BACK")
                self.assertEqual(predecessor["pointer"].read_bytes(), old_pointer)
                self.assertEqual(cutover._payload_update_witnesses(fx["target"], fx["state_dir"], predecessor["current_plist"]), witnesses)
                self.assertFalse(fx["transaction"].exists())
                self.assertFalse((fx["state_dir"] / "payloads" / published["content_closure"]).exists())
                # An unverified incoming release must fail before another pointer switch.
                invalid = incoming / "agent_runtime/server.py"
                invalid.chmod(0o644)
                invalid.write_text("UNVERIFIED = True\n")
                with self.assertRaises(cutover.CutoverError):
                    cutover.activate_payload_release(
                        incoming, target_app=fx["target"], state_dir=fx["state_dir"],
                        transaction_dir=fx["transaction"], home=fx["home"], launchctl=fx["launchctl"], uid=os.getuid(),
                    )
                self.assertEqual(predecessor["pointer"].read_bytes(), old_pointer)
                self.assertFalse(fx["transaction"].exists())
