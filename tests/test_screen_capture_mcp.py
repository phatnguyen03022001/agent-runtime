from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.types import ImageContent, TextContent

from agent_runtime import screen_capture as screen_capture_feature
from agent_runtime import server
from agent_runtime.timing import ALLOWED_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = (
    "terminal_exec",
    "terminal_start",
    "terminal_poll",
    "terminal_control",
    "terminal_resize",
    "capacity_observer",
    "fs_read_batch",
    "fs_list",
    "fs_search",
    "fs_patch",
    "fs_write",
    "repo_observer",
    "repo_diff",
    "repo_stage",
    "repo_commit",
    "repo_fast_forward",
    "repo_publish",
    "screen_capture",
    "runtime_capabilities",
)
EXPECTED_ANNOTATIONS = {
    "terminal_exec": (False, True, False, True),
    "terminal_start": (False, True, False, True),
    "terminal_poll": (False, False, False, False),
    "terminal_control": (False, True, False, True),
    "terminal_resize": (False, False, True, False),
    "capacity_observer": (True, False, True, False),
    "fs_read_batch": (True, False, True, False),
    "fs_list": (True, False, True, False),
    "fs_search": (True, False, True, False),
    "fs_patch": (False, True, False, False),
    "fs_write": (False, True, False, False),
    "repo_observer": (True, False, True, False),
    "repo_diff": (True, False, True, False),
    "repo_stage": (False, True, False, False),
    "repo_commit": (False, True, False, False),
    "repo_fast_forward": (False, True, True, True),
    "repo_publish": (False, True, True, True),
    "screen_capture": (True, False, True, False),
    "runtime_capabilities": (True, False, True, False),
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


class ScreenCaptureMCPTests(unittest.IsolatedAsyncioTestCase):
    async def _tools(self) -> dict[str, object]:
        return {tool.name: tool for tool in await server.mcp.list_tools()}

    async def test_screen_capture_is_exact_public_tool_18_with_annotations(self) -> None:
        tools = await self._tools()
        self.assertEqual(tuple(tools), EXPECTED_TOOLS)
        for name, tool in tools.items():
            self.assertEqual(_annotations(tool), EXPECTED_ANNOTATIONS[name])
        self.assertEqual(tuple(tools)[:-1], EXPECTED_TOOLS[:-1])
        self.assertEqual(tuple(tools)[-2], "screen_capture")
        self.assertEqual(tuple(tools)[-1], "runtime_capabilities")

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

    async def test_screen_capture_is_blocked_before_argument_validation(self) -> None:
        result = await server.mcp.call_tool("screen_capture", {"target": "window"})
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertEqual(len(result.content), 1)
        self.assertIsInstance(result.content[0], TextContent)
        text = result.content[0].text
        error = json.loads(text)
        self.assertEqual(text, json.dumps(error, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        self.assertEqual(set(error), {"code", "message", "retryable"})
        self.assertEqual(error["code"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertFalse(error["retryable"])
        self.assertLessEqual(len(error["message"]), 256)
        self.assertLessEqual(len(text.encode("utf-8")), screen_capture_feature.MAX_HEADER_BYTES)

    async def test_production_guard_skips_capture_delegate_and_native_helper(self) -> None:
        with patch.object(server, "capture_screen", side_effect=AssertionError("capture delegate invoked")) as capture:
            with patch.object(
                screen_capture_feature,
                "_invoke_helper",
                side_effect=AssertionError("native helper invoked"),
            ) as helper:
                result = await server.mcp.call_tool("screen_capture", {})

        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        images = [block for block in result.content if isinstance(block, ImageContent)]
        texts = [block for block in result.content if isinstance(block, TextContent)]
        self.assertEqual(images, [])
        self.assertEqual(len(texts), 1)
        error = json.loads(texts[0].text)
        self.assertEqual(error["code"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertFalse(error["retryable"])
        capture.assert_not_called()
        helper.assert_not_called()

    async def test_timing_and_instructions_expose_only_read_capture_boundary(self) -> None:
        self.assertIn("screen_capture", ALLOWED_TOOL_NAMES)
        self.assertIn("screen_capture", server.SERVER_INSTRUCTIONS)
        self.assertIn("governance-blocked", server.SERVER_INSTRUCTIONS)
        self.assertIn("VISUAL_PERCEPTION_BLOCKED", server.SERVER_INSTRUCTIONS)
        self.assertIn("before native capture", server.SERVER_INSTRUCTIONS)
        self.assertIn("future Architect re-authorization", server.SERVER_INSTRUCTIONS)
        self.assertIn("new source verification, packaging, and activation", server.SERVER_INSTRUCTIONS)
        self.assertIn("never requested automatically", server.SERVER_INSTRUCTIONS)

    def test_readme_documents_exact_eleven_tool_surface(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("exactly nineteen public tools", readme)
        self.assertIn("screen_capture", readme)
        self.assertIn("Screen Recording", readme)
        self.assertIn("cg_global_points", readme)


if __name__ == "__main__":
    unittest.main()
