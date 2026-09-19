from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_runtime import screen_capture
from mcp.types import ImageContent, TextContent

ROOT = Path(__file__).resolve().parents[1]
PNG = (ROOT / "tests" / "fixtures" / "task0087-tiny.png").read_bytes()


def _metadata(**overrides: object) -> dict[str, object]:
    app = {"pid": 123, "bundle_identifier": "com.example.app", "name": "Example"}
    value: dict[str, object] = {
        "schema_version": 1,
        "status": "captured",
        "target": "frontmost_window",
        "mime_type": "image/png",
        "raw_bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
        "coordinate_space": "cg_global_points",
        "bounds": {"x": -10.0, "y": 20.0, "width": 1.0, "height": 1.0},
        "pixel_width": 2,
        "pixel_height": 2,
        "scale_factor": 2.0,
        "display_id": 1,
        "window_id": 99,
        "active_application": app,
        "captured_application": app,
        "permission": "granted",
        "capture_api": "ScreenCaptureKit",
        "deadline_seconds": screen_capture.DEADLINE_SECONDS,
    }
    value.update(overrides)
    return value


def _framed(metadata: dict[str, object], payload: bytes = PNG) -> bytes:
    return (
        json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
        + payload
    )


class ScreenCaptureTests(unittest.TestCase):
    def test_fixed_helper_path_is_derived_from_installed_runtime_layout(self) -> None:
        fake_file = (
            Path("/tmp/Agent Runtime.app/Contents/Resources/runtime")
            / "agent_runtime"
            / "screen_capture.py"
        )
        with patch.object(screen_capture, "__file__", str(fake_file)):
            self.assertEqual(
                screen_capture._fixed_helper_path(),
                Path("/tmp/Agent Runtime.app/Contents/MacOS/AgentRuntimeScreenCapture").resolve(),
            )

    def test_request_cross_field_rules_fail_closed(self) -> None:
        bad = [
            ("frontmost_window", 1, None, None, None, None, None, None),
            ("window", None, None, None, None, None, None, None),
            ("application_window", None, None, None, None, None, None, None),
            ("display", None, None, None, None, None, None, None),
            ("region", None, None, 1, 0.0, 0.0, 10.0, None),
        ]
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
                    screen_capture._validate_request(*args)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
    def test_parser_preserves_png_hash_and_coordinate_metadata(self) -> None:
        metadata, payload = screen_capture._parse_helper_output(0, _framed(_metadata()))
        self.assertEqual(payload, PNG)
        self.assertEqual(metadata.sha256, hashlib.sha256(PNG).hexdigest())
        self.assertEqual(metadata.bounds.x, -10.0)
        self.assertEqual(metadata.scale_factor, 2.0)
        self.assertEqual(metadata.pixel_width, 2)
        self.assertEqual(metadata.coordinate_space, "cg_global_points")

    def test_parser_rejects_bad_png_hash_and_extra_metadata(self) -> None:
        for mutation in (
            {"sha256": "0" * 64},
            {"unexpected": "field"},
        ):
            value = _metadata()
            value.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
                    screen_capture._parse_helper_output(0, _framed(value))
                self.assertEqual(caught.exception.code, "CAPTURE_PROTOCOL_ERROR")

    def test_parser_maps_typed_permission_error_without_payload(self) -> None:
        header = {
            "status": "error",
            "error_code": "SCREEN_CAPTURE_PERMISSION_REQUIRED",
            "message": "Screen Recording permission is required.",
            "retryable": False,
        }
        output = json.dumps(header, separators=(",", ":")).encode() + b"\n"
        with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
            screen_capture._parse_helper_output(2, output)
        self.assertEqual(caught.exception.code, "SCREEN_CAPTURE_PERMISSION_REQUIRED")
        self.assertFalse(caught.exception.retryable)

    def test_helper_invocation_is_fixed_bounded_and_discards_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "AgentRuntimeScreenCapture"
            helper.write_bytes(b"helper")
            helper.chmod(0o755)
            completed = SimpleNamespace(returncode=0, stdout=_framed(_metadata()))
            with patch.object(screen_capture.subprocess, "run", return_value=completed) as run:
                code, output = screen_capture._invoke_helper([str(helper), "--target", "frontmost_window"])
            self.assertEqual(code, 0)
            self.assertEqual(output, completed.stdout)
            kwargs = run.call_args.kwargs
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], screen_capture.DEADLINE_SECONDS)

    def test_helper_stdout_over_fixed_bound_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "AgentRuntimeScreenCapture"
            helper.write_bytes(b"helper")
            helper.chmod(0o755)
            completed = SimpleNamespace(returncode=0, stdout=b"x" * 9)
            with patch.object(screen_capture, "MAX_STDOUT_BYTES", 8):
                with patch.object(screen_capture.subprocess, "run", return_value=completed):
                    with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
                        screen_capture._invoke_helper([str(helper), "--target", "frontmost_window"])
            self.assertEqual(caught.exception.code, "CAPTURE_PROTOCOL_ERROR")

    def test_helper_timeout_is_typed_and_no_temp_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "AgentRuntimeScreenCapture"
            helper.write_bytes(b"helper")
            helper.chmod(0o755)
            before = sorted(Path(tmp).iterdir())
            timeout = subprocess.TimeoutExpired([str(helper)], screen_capture.DEADLINE_SECONDS)
            with patch.object(screen_capture.subprocess, "run", side_effect=timeout):
                with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
                    screen_capture._invoke_helper([str(helper), "--target", "frontmost_window"])
            after = sorted(Path(tmp).iterdir())
            self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(before, after)

    def test_real_timeout_reaps_direct_helper_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = root / "AgentRuntimeScreenCapture"
            pid_file = root / "pid"
            helper.write_text(
                "#!/bin/sh\nprintf '%s' \"$$\" > \"$1\"\nexec /bin/sleep 5\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            with patch.object(screen_capture, "DEADLINE_SECONDS", 0.5):
                with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
                    screen_capture._invoke_helper([str(helper), str(pid_file)])
            self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
            pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_success_result_is_media_first_with_bounded_deterministic_metadata(self) -> None:
        expected = _metadata()
        with patch.object(screen_capture, "_fixed_helper_path", return_value=Path("/fixed/helper")):
            with patch.object(screen_capture, "_invoke_helper", return_value=(0, _framed(expected))):
                result = screen_capture.capture_screen(
                    "frontmost_window", None, None, None, None, None, None, None
                )
        self.assertEqual(len(result.content), 2)
        self.assertIsInstance(result.content[0], ImageContent)
        self.assertIsInstance(result.content[1], TextContent)
        self.assertEqual(result.content[0].mime_type, "image/png")
        self.assertEqual(result.structured_content, None)
        self.assertLessEqual(len(result.content[1].text.encode("utf-8")), screen_capture.MAX_HEADER_BYTES)
        self.assertEqual(
            result.content[1].text,
            json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )

    def test_metadata_text_exceeding_header_bound_fails_closed(self) -> None:
        metadata, _ = screen_capture._parse_helper_output(0, _framed(_metadata()))
        with patch.object(screen_capture, "MAX_HEADER_BYTES", 1):
            with self.assertRaises(screen_capture.ScreenCaptureFailure) as caught:
                screen_capture._metadata_text(metadata)
        self.assertEqual(caught.exception.code, "CAPTURE_PROTOCOL_ERROR")


if __name__ == "__main__":
    unittest.main()
