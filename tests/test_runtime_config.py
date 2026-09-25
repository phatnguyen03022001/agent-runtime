from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_config", ROOT / "macos" / "runtime_config.py")
assert SPEC is not None and SPEC.loader is not None
runtime_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_config)


class RuntimeConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
