from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from agent_runtime import server

ROOT = Path(__file__).resolve().parents[1]


class Task0140QualificationTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_unexpected_failure_is_sanitized(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        delegate_name: str,
        sentinel: str,
    ) -> None:
        with patch.object(server, delegate_name, side_effect=RuntimeError(sentinel)):
            with self.assertLogs("mcp.server.mcpserver.server", level="ERROR") as captured:
                async with Client(server.mcp) as client:
                    result = await client.call_tool(tool_name, arguments)

        self.assertTrue(result.is_error)
        serialized = result.model_dump_json(by_alias=True)
        self.assertIn(f"Error executing tool {tool_name}", serialized)
        self.assertNotIn(sentinel, serialized)
        self.assertTrue(
            any("unexpected runtime tool failure" in line for line in captured.output),
            "unexpected failures must remain observable through a bounded sanitized error",
        )
        self.assertFalse(
            any(sentinel in line for line in captured.output),
            "unexpected exception traceback leaked sentinel to MCP server logs",
        )

    async def test_unexpected_tool_exception_does_not_leak_secret_to_server_logs(self) -> None:
        with self.subTest(tool="terminal_exec"):
            await self._assert_unexpected_failure_is_sanitized(
                tool_name="terminal_exec",
                arguments={
                    "argv": ["/usr/bin/true"],
                    "cwd": str(ROOT),
                    "start_identity": "3" * 32,
                    "timeout_seconds": 5,
                },
                delegate_name="execute_terminal",
                sentinel="TASK0140_TERMINAL_SECRET_SENTINEL",
            )
        with self.subTest(tool="fs_list"):
            await self._assert_unexpected_failure_is_sanitized(
                tool_name="fs_list",
                arguments={"cwd": str(ROOT), "path": ".", "max_entries": 5},
                delegate_name="list_directory",
                sentinel="TASK0140_FS_LIST_SECRET_SENTINEL",
            )

    def test_every_registered_public_tool_uses_common_sanitizer(self) -> None:
        manager = server.mcp._tool_manager
        self.assertEqual(len(server.PUBLIC_TOOL_NAMES), 20)
        for name in server.PUBLIC_TOOL_NAMES:
            with self.subTest(tool=name):
                registered = manager.get_tool(name)
                self.assertIsNotNone(registered)
                sanitized = getattr(registered.fn, "__wrapped__", None)
                self.assertTrue(
                    getattr(sanitized, server._COMMON_TOOL_SANITIZER_MARKER, False)
                )


if __name__ == "__main__":
    unittest.main()
