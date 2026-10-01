from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_config", ROOT / "macos" / "runtime_config.py")
assert SPEC is not None and SPEC.loader is not None
runtime_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_config)


class RuntimeConfigTests(unittest.TestCase):
    TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"

    def _write_tunnel_client(self, temp: Path, *, output: str, exit_code: int) -> Path:
        tools = temp / "tools"
        tools.mkdir(exist_ok=True)
        client = tools / "tunnel-client"
        client.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            "  [ \"$arg\" != \"$CONTROL_PLANE_API_KEY\" ] || exit 97\n"
            "done\n"
            + "printf '%s\\n' " + repr(output) + "\n"
            + f"exit {exit_code}\n"
        )
        client.chmod(0o700)
        return client

    def _native_environment(self, client: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(client.parent)
        env.pop("OPENAI_ADMIN_KEY", None)
        return env

    def _assert_undecodable_response_is_private_admission_error(self, stream: str) -> None:
        for existing in (False, True):
            with self.subTest(stream=stream, existing=existing), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                home = root / "home"
                workspace = root / "workspace"
                home.mkdir()
                workspace.mkdir()
                canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
                secret = "UNDECODABLE_SUBMITTED_SYNTHETIC_KEY"
                marker = "UNDECODABLE_SYNTHETIC_RESPONSE_MARKER"
                values = {
                    "CONTROL_PLANE_API_KEY": secret,
                    "CONTROL_PLANE_TUNNEL_ID": self.TUNNEL_ID,
                    "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                    "AGENT_RUNTIME_GIT_NAME": "Synthetic Decoder Operator",
                    "AGENT_RUNTIME_GIT_EMAIL": "decoder@example.invalid",
                }
                before = None
                identity = None
                if existing:
                    canonical.parent.mkdir(parents=True)
                    canonical.write_bytes(runtime_config._prebuilt_payload({
                        **values, "CONTROL_PLANE_API_KEY": "PREDECESSOR_SYNTHETIC_KEY",
                    }))
                    canonical.chmod(0o600)
                    before = canonical.read_bytes()
                    info = canonical.stat()
                    identity = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
                client = self._write_tunnel_client(root, output="", exit_code=0)
                argv_capture = root / "argv.txt"
                redirect = " >&2" if stream == "stderr" else ""
                client.write_text(
                    "#!/bin/sh\n"
                    + "printf '%s\\n' \"$@\" > " + shlex.quote(str(argv_capture)) + "\n"
                    + ("printf '%s\\n' '{}'\n" if stream == "stderr" else "")
                    + f"printf '\\377{marker}%s' \"$CONTROL_PLANE_API_KEY\"{redirect}\n"
                    + "exit 0\n"
                )
                result = subprocess.run(
                    [sys.executable, str(ROOT / "macos/runtime_config.py"),
                     "--reconfigure-stdin" if existing else "--prebuilt-stdin", str(canonical)],
                    input=json.dumps(values).encode(), capture_output=True, check=False,
                    timeout=20, cwd=root,
                    env={"HOME": str(home), "PATH": str(client.parent), "TMPDIR": str(root),
                         "LANG": "C", "LC_ALL": "C", "PYTHONUTF8": "1",
                         "PYTHONDONTWRITEBYTECODE": "1"},
                )
                if existing:
                    self.assertEqual(canonical.read_bytes(), before)
                    info = canonical.stat()
                    self.assertEqual((info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)), identity)
                else:
                    self.assertFalse(canonical.exists())
                output = result.stdout + result.stderr
                self.assertNotIn(b"\xff", output)
                self.assertNotIn(marker.encode(), output)
                self.assertNotIn(secret.encode(), output)
                self.assertNotIn(secret, argv_capture.read_text())
                self.assertLess(len(output), 1024)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(json.loads(result.stdout.decode("utf-8")), {
                    "schema_version": 1,
                    "status": "error",
                    "reason_code": "CONTROL_PLANE_UNAVAILABLE",
                    "message": "Tunnel validation is temporarily unavailable. Try again later.",
                })

    def test_undecodable_stdout_maps_private_structured_error_and_preserves_config(self) -> None:
        self._assert_undecodable_response_is_private_admission_error("stdout")

    def test_undecodable_stderr_maps_private_structured_error_and_preserves_config(self) -> None:
        self._assert_undecodable_response_is_private_admission_error("stderr")

    def test_tunnel_child_environment_keeps_only_bounded_process_context_and_submitted_key(self) -> None:
        source = {
            "HOME": "/synthetic/home",
            "USER": "synthetic-user",
            "TMPDIR": "/synthetic/tmp",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/synthetic/bin",
            "CONTROL_PLANE_API_KEY": "OLD_KEY",
            "OPENAI_ADMIN_KEY": "ADMIN_SENTINEL",
            "OPENAI_API_KEY": "OTHER_SENTINEL",
            "UNRELATED_SECRET": "UNRELATED_SENTINEL",
        }
        child = runtime_config._safe_tunnel_environment(source, "SUBMITTED_RUNTIME_KEY")
        self.assertEqual(child["CONTROL_PLANE_API_KEY"], "SUBMITTED_RUNTIME_KEY")
        self.assertEqual(child["PATH"], "/synthetic/bin")
        self.assertEqual(child["LC_ALL"], "C")
        self.assertNotIn("OPENAI_ADMIN_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("UNRELATED_SECRET", child)

    def _write_source(self, path: Path, workspace_value: str) -> None:
        path.write_text(
            "CONTROL_PLANE_API_KEY=test-key\n"
            "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
            f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace_value}\n"
            "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n"
        )
        path.chmod(0o600)

    def test_first_bootstrap_persists_derived_workspace_root_over_blank_stale_or_different_source(self) -> None:
        for source_value_kind in ("blank", "stale", "different"):
            with self.subTest(source_value_kind=source_value_kind), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                derived = temp / "workspace"
                derived.mkdir()
                different = temp / "different"
                different.mkdir()
                source_value = {
                    "blank": "",
                    "stale": str(temp / "missing"),
                    "different": str(different),
                }[source_value_kind]
                source = temp / "source.env"
                canonical = temp / "config" / "runtime.env"
                self._write_source(source, source_value)

                runtime_config.ensure(source, canonical, derived)

                text = canonical.read_text()
                self.assertIn(f"AGENT_RUNTIME_WORKSPACE_ROOT={derived}\n", text)
                self.assertIn("CONTROL_PLANE_API_KEY=test-key\n", text)
                self.assertIn("CONTROL_PLANE_TUNNEL_ID=test-tunnel\n", text)
                self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n", text)
                self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)

    def test_existing_canonical_file_is_byte_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            canonical_workspace = temp / "canonical-workspace"
            canonical_workspace.mkdir()
            derived = temp / "derived-workspace"
            derived.mkdir()
            source = temp / "source.env"
            self._write_source(source, "")
            canonical = temp / "config" / "runtime.env"
            canonical.parent.mkdir()
            self._write_source(canonical, str(canonical_workspace))
            before = canonical.read_bytes()

            runtime_config.ensure(source, canonical, derived)

            self.assertEqual(canonical.read_bytes(), before)

    def test_first_publication_is_complete_validated_and_fsynced_before_final_path_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            derived = temp / "workspace"
            derived.mkdir()
            source = temp / "source.env"
            canonical = temp / "config" / "runtime.env"
            self._write_source(source, "")
            expected = source.read_text().replace(
                "AGENT_RUNTIME_WORKSPACE_ROOT=\n",
                f"AGENT_RUNTIME_WORKSPACE_ROOT={derived}\n",
            ).encode()
            real_fsync = runtime_config.os.fsync
            real_validate = runtime_config.validate
            real_link = runtime_config.os.link
            fsynced = False
            validated: set[Path] = set()

            def record_fsync(fd: int) -> None:
                nonlocal fsynced
                real_fsync(fd)
                fsynced = True

            def record_validate(path: Path, *, require_mode: bool) -> bytes:
                result = real_validate(path, require_mode=require_mode)
                validated.add(path)
                return result

            def publish(source_path: str | bytes, destination_path: str | bytes) -> None:
                private = Path(source_path)
                self.assertFalse(canonical.exists())
                self.assertNotEqual(private, canonical)
                self.assertEqual(private.read_bytes(), expected)
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
                self.assertTrue(fsynced)
                self.assertIn(private, validated)
                real_link(source_path, destination_path)

            with mock.patch.object(runtime_config.os, "fsync", side_effect=record_fsync), mock.patch.object(
                runtime_config, "validate", side_effect=record_validate
            ), mock.patch.object(runtime_config.os, "link", side_effect=publish) as link:
                runtime_config.ensure(source, canonical, derived)

            self.assertEqual(link.call_count, 1)
            self.assertEqual(canonical.read_bytes(), expected)

    def test_failed_publication_leaves_no_partial_final_or_private_temp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            derived = temp / "workspace"
            derived.mkdir()
            source = temp / "source.env"
            canonical = temp / "config" / "runtime.env"
            self._write_source(source, "")

            with mock.patch.object(runtime_config.os, "link", side_effect=OSError("injected publish failure")):
                with self.assertRaisesRegex(OSError, "injected publish failure"):
                    runtime_config.ensure(source, canonical, derived)

            self.assertFalse(canonical.exists())
            self.assertEqual(list(canonical.parent.iterdir()), [])

    def test_interrupted_publication_cleans_private_temp_and_leaves_no_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            derived = temp / "workspace"
            derived.mkdir()
            source = temp / "source.env"
            canonical = temp / "config" / "runtime.env"
            self._write_source(source, "")

            with mock.patch.object(runtime_config.os, "link", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    runtime_config.ensure(source, canonical, derived)

            self.assertFalse(canonical.exists())
            self.assertEqual(list(canonical.parent.iterdir()), [])

    def test_concurrent_valid_canonical_file_wins_without_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            derived = temp / "derived"
            derived.mkdir()
            winner_workspace = temp / "winner"
            winner_workspace.mkdir()
            source = temp / "source.env"
            canonical = temp / "config" / "runtime.env"
            self._write_source(source, "")
            winner = (
                "CONTROL_PLANE_API_KEY=winner-key\n"
                "CONTROL_PLANE_TUNNEL_ID=winner-tunnel\n"
                f"AGENT_RUNTIME_WORKSPACE_ROOT={winner_workspace}\n"
            ).encode()

            def concurrent_publish(_source_path: str | bytes, _destination_path: str | bytes) -> None:
                canonical.write_bytes(winner)
                canonical.chmod(0o600)
                raise FileExistsError

            with mock.patch.object(runtime_config.os, "link", side_effect=concurrent_publish):
                runtime_config.ensure(source, canonical, derived)

            self.assertEqual(canonical.read_bytes(), winner)

    def test_parallelism_limit_accepts_absent_one_two_and_ten(self) -> None:
        for configured in (None, "1", "2", "10"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                workspace = temp / "workspace"
                workspace.mkdir()
                canonical = temp / "runtime.env"
                text = (
                    "CONTROL_PLANE_API_KEY=test-key\n"
                    "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                    f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                )
                if configured is not None:
                    text += f"AGENT_RUNTIME_MAX_PARALLELISM={configured}\n"
                canonical.write_text(text)
                canonical.chmod(0o600)
                runtime_config.validate(canonical, require_mode=True)

    def test_parallelism_limit_rejects_malformed_and_out_of_range_values(self) -> None:
        for configured in ("", "0", "11", "-1", "2.0", "many"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                workspace = temp / "workspace"
                workspace.mkdir()
                canonical = temp / "runtime.env"
                canonical.write_text(
                    "CONTROL_PLANE_API_KEY=test-key\n"
                    "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                    f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                    f"AGENT_RUNTIME_MAX_PARALLELISM={configured}\n"
                )
                canonical.chmod(0o600)
                with self.assertRaisesRegex(SystemExit, "AGENT_RUNTIME_MAX_PARALLELISM"):
                    runtime_config.validate(canonical, require_mode=True)

    def test_session_limit_rejects_values_outside_the_x6_contract(self) -> None:
        for configured in ("", "0", "7", "22", "many"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                workspace = temp / "workspace"
                workspace.mkdir()
                canonical = temp / "runtime.env"
                canonical.write_text(
                    "CONTROL_PLANE_API_KEY=test-key\n"
                    "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                    f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                    f"AGENT_RUNTIME_MAX_ACTIVE_SESSIONS={configured}\n"
                )
                canonical.chmod(0o600)
                with self.assertRaisesRegex(SystemExit, "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS"):
                    runtime_config.validate(canonical, require_mode=True)

    def test_telemetry_mode_accepts_absent_off_and_otlp_only(self) -> None:
        for configured in (None, "off", "otlp"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                workspace = temp / "workspace"
                workspace.mkdir()
                canonical = temp / "runtime.env"
                text = (
                    "CONTROL_PLANE_API_KEY=test-key\n"
                    "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                    f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                )
                if configured is not None:
                    text += f"AGENT_RUNTIME_TELEMETRY={configured}\n"
                canonical.write_text(text)
                canonical.chmod(0o600)
                runtime_config.validate(canonical, require_mode=True)

    def test_telemetry_mode_rejects_empty_remote_and_arbitrary_values(self) -> None:
        for configured in ("", "on", "http://collector.example", "grpc", "OTLP"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                workspace = temp / "workspace"
                workspace.mkdir()
                canonical = temp / "runtime.env"
                canonical.write_text(
                    "CONTROL_PLANE_API_KEY=test-key\n"
                    "CONTROL_PLANE_TUNNEL_ID=test-tunnel\n"
                    f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace}\n"
                    f"AGENT_RUNTIME_TELEMETRY={configured}\n"
                )
                canonical.chmod(0o600)
                with self.assertRaisesRegex(SystemExit, "AGENT_RUNTIME_TELEMETRY"):
                    runtime_config.validate(canonical, require_mode=True)

    def test_first_bootstrap_fills_empty_secrets_from_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            source = temp / "source.env"
            source.write_text(
                "CONTROL_PLANE_API_KEY=\n"
                "CONTROL_PLANE_TUNNEL_ID=\n"
                "AGENT_RUNTIME_WORKSPACE_ROOT=\n"
            )
            canonical = temp / "config" / "runtime.env"
            with mock.patch.dict(
                runtime_config.os.environ,
                {"CONTROL_PLANE_API_KEY": "env-key", "CONTROL_PLANE_TUNNEL_ID": "env-tunnel"},
                clear=False,
            ):
                try:
                    runtime_config.ensure(source, canonical, workspace)
                except SystemExit as exc:
                    self.fail(f"process-environment fallback was not applied: {exc}")
            text = canonical.read_text()
            self.assertIn("CONTROL_PLANE_API_KEY=env-key\n", text)
            self.assertIn("CONTROL_PLANE_TUNNEL_ID=env-tunnel\n", text)
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)

    def test_first_bootstrap_source_secrets_win_over_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            source = temp / "source.env"
            self._write_source(source, "")
            canonical = temp / "config" / "runtime.env"
            with mock.patch.dict(
                runtime_config.os.environ,
                {"CONTROL_PLANE_API_KEY": "env-key", "CONTROL_PLANE_TUNNEL_ID": "env-tunnel"},
                clear=False,
            ):
                runtime_config.ensure(source, canonical, workspace)
            text = canonical.read_text()
            self.assertIn("CONTROL_PLANE_API_KEY=test-key\n", text)
            self.assertIn("CONTROL_PLANE_TUNNEL_ID=test-tunnel\n", text)
            self.assertNotIn("env-key", text)
            self.assertNotIn("env-tunnel", text)

    def test_existing_canonical_ignores_process_environment_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            source = temp / "source.env"
            self._write_source(source, "")
            canonical = temp / "config" / "runtime.env"
            canonical.parent.mkdir()
            self._write_source(canonical, str(workspace))
            before = canonical.read_bytes()
            with mock.patch.dict(
                runtime_config.os.environ,
                {"CONTROL_PLANE_API_KEY": "env-key", "CONTROL_PLANE_TUNNEL_ID": "env-tunnel"},
                clear=False,
            ):
                runtime_config.ensure(source, canonical, workspace)
            self.assertEqual(canonical.read_bytes(), before)

    def test_prebuilt_bootstrap_requires_explicit_provisioning_and_writes_private_canonical_config(self) -> None:
        self.assertTrue(hasattr(runtime_config, "ensure_prebuilt"), "prebuilt config entrypoint must exist")
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "config" / "runtime.env"
            environ = {
                "CONTROL_PLANE_API_KEY": "PREBUILT_SECRET_API",
                "CONTROL_PLANE_TUNNEL_ID": self.TUNNEL_ID,
                "AGENT_RUNTIME_GIT_NAME": "Prebuilt Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "prebuilt@example.invalid",
                "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS": "6",
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
            }
            client = self._write_tunnel_client(
                temp,
                output=json.dumps({"id": self.TUNNEL_ID}),
                exit_code=0,
            )
            environ["PATH"] = str(client.parent)

            runtime_config.ensure_prebuilt(canonical, workspace, environ=environ)

            text = canonical.read_text()
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)
            self.assertIn("CONTROL_PLANE_API_KEY=PREBUILT_SECRET_API\n", text)
            self.assertIn(f"CONTROL_PLANE_TUNNEL_ID={self.TUNNEL_ID}\n", text)
            self.assertIn(f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace.resolve()}\n", text)
            self.assertIn("AGENT_RUNTIME_GIT_NAME=Prebuilt Operator\n", text)
            self.assertIn("AGENT_RUNTIME_GIT_EMAIL=prebuilt@example.invalid\n", text)

            before = canonical.read_bytes()
            runtime_config.ensure_prebuilt(canonical, workspace, environ={})
            self.assertEqual(canonical.read_bytes(), before)

    def test_prebuilt_bootstrap_rejects_missing_workspace_or_identity_without_disclosing_secrets(self) -> None:
        self.assertTrue(hasattr(runtime_config, "ensure_prebuilt"), "prebuilt config entrypoint must exist")
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "runtime.env"
            secret = "DO_NOT_DISCLOSE_PREBUILT_SECRET"
            environ = {
                "CONTROL_PLANE_API_KEY": secret,
                "CONTROL_PLANE_TUNNEL_ID": self.TUNNEL_ID,
                "AGENT_RUNTIME_GIT_NAME": "Prebuilt Operator",
            }
            with self.assertRaises(SystemExit) as raised:
                runtime_config.ensure_prebuilt(canonical, workspace, environ=environ)
            self.assertNotIn(secret, str(raised.exception))
            self.assertFalse(canonical.exists())

            missing_workspace = temp / "missing"
            complete = {
                **environ,
                "AGENT_RUNTIME_GIT_EMAIL": "prebuilt@example.invalid",
            }
            with self.assertRaisesRegex(SystemExit, "absolute existing directory"):
                runtime_config.ensure_prebuilt(canonical, missing_workspace, environ=complete)


    def test_native_stdin_requires_tunnel_client_before_canonical_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "config" / "runtime.env"
            secret = "NATIVE_SENTINEL_" + uuid.uuid4().hex
            payload = {
                "CONTROL_PLANE_API_KEY": secret,
                "CONTROL_PLANE_TUNNEL_ID": "tunnel_0123456789abcdef0123456789abcdef",
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Native Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "native@example.invalid",
            }
            env = os.environ.copy()
            env["PATH"] = str(temp / "missing-tools")

            result = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--prebuilt-stdin", str(canonical)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["reason_code"], "TUNNEL_CLIENT_UNAVAILABLE")
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertFalse(canonical.exists())

    def test_native_stdin_bootstrap_is_private_atomic_and_non_echoing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "config" / "runtime.env"
            api_secret = "NATIVE_SENTINEL_" + uuid.uuid4().hex
            tunnel_secret = self.TUNNEL_ID
            client = self._write_tunnel_client(
                temp,
                output=json.dumps({"id": tunnel_secret}),
                exit_code=0,
            )
            payload = {
                "CONTROL_PLANE_API_KEY": api_secret,
                "CONTROL_PLANE_TUNNEL_ID": tunnel_secret,
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Native Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "native@example.invalid",
            }

            result = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--prebuilt-stdin", str(canonical)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
                env=self._native_environment(client),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(api_secret, result.stdout + result.stderr)
            self.assertNotIn(tunnel_secret, result.stdout + result.stderr)
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)
            self.assertIn(f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace.resolve()}\n", canonical.read_text())
            self.assertIn(f"CONTROL_PLANE_API_KEY={api_secret}\n", canonical.read_text())
            self.assertEqual([path.name for path in canonical.parent.iterdir()], ["runtime.env"])

            inspected = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--inspect-prebuilt-existing", str(canonical)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertNotIn(api_secret, inspected.stdout + inspected.stderr)
            self.assertNotIn(tunnel_secret, inspected.stdout + inspected.stderr)
            self.assertEqual(
                json.loads(inspected.stdout),
                {"git_identity_ready": True, "workspace_root": str(workspace.resolve())},
            )

    def test_native_stdin_maps_structured_tunnel_failures_without_publishing(self) -> None:
        cases = (
            (401, "INVALID_CREDENTIAL"),
            (403, "TUNNEL_ACCESS_DENIED"),
            (404, "TUNNEL_NOT_FOUND"),
            (503, "CONTROL_PLANE_UNAVAILABLE"),
        )
        for status, reason in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                workspace = temp / "workspace"
                workspace.mkdir()
                canonical = temp / "config" / "runtime.env"
                secret = "FAILURE_SENTINEL_" + uuid.uuid4().hex
                client = self._write_tunnel_client(
                    temp,
                    output=json.dumps({"error": {"status": status}}),
                    exit_code=1,
                )
                payload = {
                    "CONTROL_PLANE_API_KEY": secret,
                    "CONTROL_PLANE_TUNNEL_ID": self.TUNNEL_ID,
                    "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                    "AGENT_RUNTIME_GIT_NAME": "Native Operator",
                    "AGENT_RUNTIME_GIT_EMAIL": "native@example.invalid",
                }
                result = subprocess.run(
                    [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--prebuilt-stdin", str(canonical)],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._native_environment(client),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout)["reason_code"], reason)
                self.assertNotIn(secret, result.stdout + result.stderr)
                self.assertFalse(canonical.exists())

    def test_native_stdin_rejects_malformed_tunnel_response_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "runtime.env"
            client = self._write_tunnel_client(temp, output="not-json", exit_code=0)
            payload = {
                "CONTROL_PLANE_API_KEY": "MALFORMED_SENTINEL",
                "CONTROL_PLANE_TUNNEL_ID": self.TUNNEL_ID,
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Native Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "native@example.invalid",
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--prebuilt-stdin", str(canonical)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
                env=self._native_environment(client),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["reason_code"], "CONTROL_PLANE_UNAVAILABLE")
            self.assertFalse(canonical.exists())

    def test_reconfigure_validates_before_atomic_replacement_and_preserves_previous_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "config" / "runtime.env"
            canonical.parent.mkdir()
            old_values = {
                "CONTROL_PLANE_API_KEY": "OLD_SYNTHETIC_KEY",
                "CONTROL_PLANE_TUNNEL_ID": self.TUNNEL_ID,
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Old Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "old@example.invalid",
            }
            canonical.write_bytes(runtime_config._prebuilt_payload(old_values))
            canonical.chmod(0o600)
            before = canonical.read_bytes()
            failing = self._write_tunnel_client(
                temp,
                output=json.dumps({"error": {"status": 403}}),
                exit_code=1,
            )
            new_values = {
                **old_values,
                "CONTROL_PLANE_API_KEY": "NEW_SYNTHETIC_KEY",
                "AGENT_RUNTIME_GIT_NAME": "New Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "new@example.invalid",
            }
            failed = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--reconfigure-stdin", str(canonical)],
                input=json.dumps(new_values),
                capture_output=True,
                text=True,
                check=False,
                env=self._native_environment(failing),
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(failed.stdout)["reason_code"], "TUNNEL_ACCESS_DENIED")
            self.assertEqual(canonical.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)

            success = self._write_tunnel_client(
                temp,
                output=json.dumps({"id": self.TUNNEL_ID}),
                exit_code=0,
            )
            passed = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--reconfigure-stdin", str(canonical)],
                input=json.dumps(new_values),
                capture_output=True,
                text=True,
                check=False,
                env=self._native_environment(success),
            )
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertEqual(json.loads(passed.stdout)["reason_code"], "OK")
            self.assertNotEqual(canonical.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)
            self.assertIn(b"AGENT_RUNTIME_GIT_NAME=New Operator\n", canonical.read_bytes())

    def test_native_stdin_invalid_input_leaves_no_canonical_or_secret_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "config" / "runtime.env"
            secret = "INVALID_NATIVE_SENTINEL_" + uuid.uuid4().hex
            payload = {
                "CONTROL_PLANE_API_KEY": secret,
                "CONTROL_PLANE_TUNNEL_ID": "fixture-tunnel",
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Missing Email",
            }

            result = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--prebuilt-stdin", str(canonical)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertFalse(canonical.exists())
            self.assertFalse(canonical.parent.exists())

    def test_native_stdin_rejects_unknown_configuration_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            workspace = temp / "workspace"
            workspace.mkdir()
            canonical = temp / "runtime.env"
            payload = {
                "CONTROL_PLANE_API_KEY": "api",
                "CONTROL_PLANE_TUNNEL_ID": "tunnel",
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "Native Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "native@example.invalid",
                "UNRECOGNIZED": "value",
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "macos" / "runtime_config.py"), "--prebuilt-stdin", str(canonical)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(canonical.exists())
            failure = json.loads(result.stdout)
            self.assertEqual(failure["reason_code"], "RUNTIME_CONFIGURATION_INCOMPLETE")
            self.assertIn("unsupported configuration field", failure["message"])


if __name__ == "__main__":
    unittest.main()
