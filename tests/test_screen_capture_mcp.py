from __future__ import annotations

import base64
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.types import ImageContent, TextContent

from agent_runtime import screen_capture as screen_capture_feature
from agent_runtime import server
from agent_runtime.timing import ALLOWED_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
PNG = (ROOT / "tests" / "fixtures" / "task0087-tiny.png").read_bytes()
EXPECTED_TOOLS = (
    "terminal_exec", "terminal_start", "terminal_poll", "terminal_control",
    "terminal_resize", "capacity_observer", "fs_read_batch", "repo_observer",
    "repo_fast_forward", "repo_publish", "screen_capture",
)
EXPECTED_ANNOTATIONS = {
    "terminal_exec": (False, True, False, True),
    "terminal_start": (False, True, False, True),
    "terminal_poll": (False, False, False, False),
    "terminal_control": (False, True, False, True),
    "terminal_resize": (False, False, True, False),
    "capacity_observer": (True, False, True, False),
    "fs_read_batch": (True, False, True, False),
    "repo_observer": (True, False, True, False),
    "repo_fast_forward": (False, True, True, True),
    "repo_publish": (False, True, True, True),
    "screen_capture": (True, False, True, False),
}
INPUT_FIELDS = {
    "target", "window_id", "application_bundle_id", "display_id",
    "x", "y", "width", "height",
}


def _annotations(tool: object) -> tuple[bool, bool, bool, bool]:
    values = getattr(tool, "annotations").model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


def _framed(metadata: dict[str, object]) -> bytes:
    return (
        json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
        + PNG
    )


def _metadata() -> dict[str, object]:
    app = {"pid": 123, "bundle_identifier": "com.example.app", "name": "Example"}
    return {
        "schema_version": 1, "status": "captured", "target": "frontmost_window",
        "mime_type": "image/png", "raw_bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
        "coordinate_space": "cg_global_points",
        "bounds": {"x": -10.0, "y": 20.0, "width": 1.0, "height": 1.0},
        "pixel_width": 2, "pixel_height": 2, "scale_factor": 2.0,
        "display_id": 1, "window_id": 99,
        "active_application": app, "captured_application": app,
        "permission": "granted", "capture_api": "ScreenCaptureKit",
        "deadline_seconds": 5.0,
    }


class ScreenCaptureMCPTests(unittest.IsolatedAsyncioTestCase):
    async def _tools(self) -> dict[str, object]:
        return {tool.name: tool for tool in await server.mcp.list_tools()}

    async def test_screen_capture_is_exact_public_tool_11_with_annotations(self) -> None:
        tools = await self._tools()
        self.assertEqual(tuple(tools), EXPECTED_TOOLS)
        for name, tool in tools.items():
            self.assertEqual(_annotations(tool), EXPECTED_ANNOTATIONS[name])
        self.assertEqual(tuple(tools)[:10], EXPECTED_TOOLS[:10])

    async def test_input_schema_is_closed_and_exact(self) -> None:
        schema = (await self._tools())["screen_capture"].input_schema
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), INPUT_FIELDS)
        self.assertEqual(schema["properties"]["target"]["default"], "frontmost_window")
        self.assertEqual(
            set(schema["properties"]["target"]["enum"]),
            {"frontmost_window", "window", "application_window", "display", "region"},
        )
        self.assertEqual(schema.get("required", []), [])
        for forbidden in (
            "cwd", "argv", "command", "executable", "helper", "output_path",
            "timeout", "codec", "scale", "cursor", "audio",
        ):
            self.assertNotIn(forbidden, schema["properties"])

    async def test_output_schema_is_none_for_media_first_result(self) -> None:
        schema = (await self._tools())["screen_capture"].output_schema
        self.assertIsNone(schema)

    async def test_cross_field_failure_is_typed_bounded_error(self) -> None:
        result = await server.mcp.call_tool("screen_capture", {"target": "window"})
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertEqual(len(result.content), 1)
        self.assertIsInstance(result.content[0], TextContent)
        text = result.content[0].text
        error = json.loads(text)
        self.assertEqual(text, json.dumps(error, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        self.assertEqual(set(error), {"code", "message", "retryable"})
        self.assertEqual(error["code"], "INVALID_ARGUMENT")
        self.assertLessEqual(len(error["message"]), 256)
        self.assertLessEqual(len(text.encode("utf-8")), screen_capture_feature.MAX_HEADER_BYTES)

    async def test_success_is_one_byte_exact_png_then_one_metadata_text(self) -> None:
        expected = _metadata()
        with patch.object(
            screen_capture_feature,
            "_invoke_helper",
            return_value=(0, _framed(expected)),
        ):
            result = await server.mcp.call_tool("screen_capture", {})
        self.assertFalse(result.is_error)
        images = [block for block in result.content if isinstance(block, ImageContent)]
        texts = [block for block in result.content if isinstance(block, TextContent)]
        self.assertEqual(len(images), 1)
        self.assertEqual(len(texts), 1)
        self.assertIs(result.content[0], images[0])
        self.assertIs(result.content[1], texts[0])
        self.assertEqual(images[0].mime_type, "image/png")
        decoded = base64.b64decode(images[0].data, validate=True)
        self.assertEqual(decoded, PNG)
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), expected["sha256"])
        self.assertEqual(result.structured_content, None)
        self.assertEqual(
            texts[0].text,
            json.dumps(expected, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
        self.assertLessEqual(len(texts[0].text.encode("utf-8")), screen_capture_feature.MAX_HEADER_BYTES)

    async def test_timing_and_instructions_expose_only_read_capture_boundary(self) -> None:
        self.assertIn("screen_capture", ALLOWED_TOOL_NAMES)
        self.assertIn("screen_capture", server.SERVER_INSTRUCTIONS)
        self.assertIn("ScreenCaptureKit", server.SERVER_INSTRUCTIONS)
        self.assertIn("cg_global_points", server.SERVER_INSTRUCTIONS)
        self.assertIn("never requested automatically", server.SERVER_INSTRUCTIONS)

    def test_readme_documents_exact_eleven_tool_surface(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("exactly eleven public tools", readme)
        self.assertIn("screen_capture", readme)
        self.assertIn("Screen Recording", readme)
        self.assertIn("cg_global_points", readme)


if __name__ == "__main__":
    unittest.main()
