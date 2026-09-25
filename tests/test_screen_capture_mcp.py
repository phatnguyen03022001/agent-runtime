from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
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
    "fs_manage",
    "repo_observer",
    "repo_remote_observer",
    "repo_diff",
    "repo_stage",
    "repo_commit",
    "repo_fast_forward",
    "repo_publish",
    "runtime_capabilities",
)


class ScreenCaptureMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_capture_is_not_advertised_or_registered(self) -> None:
        tools = await server.mcp.list_tools()
        self.assertEqual(tuple(tool.name for tool in tools), EXPECTED_TOOLS)
        self.assertNotIn("screen_capture", server.PUBLIC_TOOL_NAMES)
        self.assertNotIn("screen_capture", tuple(tool.name for tool in tools))

    async def test_direct_mcp_name_call_cannot_reach_capture_delegate_or_native_helper(self) -> None:
        with patch.object(server, "capture_screen", side_effect=AssertionError("capture delegate invoked")) as capture:
            with patch.object(
                screen_capture_feature,
                "_invoke_helper",
                side_effect=AssertionError("native helper invoked"),
            ) as helper:
                with self.assertRaisesRegex(ToolError, "Unknown tool: screen_capture"):
                    await server.mcp.call_tool("screen_capture", {})

        capture.assert_not_called()
        helper.assert_not_called()

    def test_retained_internal_guard_still_returns_visual_perception_blocked(self) -> None:
        with patch.object(server, "capture_screen", side_effect=AssertionError("capture delegate invoked")) as capture:
            with patch.object(
                screen_capture_feature,
                "_invoke_helper",
                side_effect=AssertionError("native helper invoked"),
            ) as helper:
                result = server.screen_capture()

        self.assertTrue(result.is_error)
        images = [block for block in result.content if isinstance(block, ImageContent)]
        texts = [block for block in result.content if isinstance(block, TextContent)]
        self.assertEqual(images, [])
        self.assertEqual(len(texts), 1)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "UNAVAILABLE")
        self.assertEqual(error["reason_code"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertFalse(error["retryable"])
        self.assertEqual(error["effect_state"], "absent")
        self.assertFalse(error["reconciliation_required"])
        self.assertEqual(error["safe_next_action"], "unsupported")
        capture.assert_not_called()
        helper.assert_not_called()

    def test_timing_and_instructions_distinguish_hidden_known_capability(self) -> None:
        self.assertNotIn("screen_capture", ALLOWED_TOOL_NAMES)
        self.assertIn("exactly twenty tools", server.SERVER_INSTRUCTIONS)
        self.assertIn("screen_capture", server.SERVER_INSTRUCTIONS)
        self.assertIn("VISUAL_PERCEPTION_BLOCKED", server.SERVER_INSTRUCTIONS)
        self.assertIn("not advertised or callable through MCP", server.SERVER_INSTRUCTIONS)
        self.assertIn("Screen Recording permission is never requested automatically", server.SERVER_INSTRUCTIONS)

    def test_readme_documents_twenty_advertised_and_twenty_one_known_capabilities(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("exactly twenty public tools", readme)
        self.assertIn("twenty-one known capabilities", readme)
        self.assertIn("screen_capture", readme)
        self.assertIn("Screen Recording", readme)


if __name__ == "__main__":
    unittest.main()
