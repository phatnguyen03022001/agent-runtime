from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from agent_runtime import server

ROOT = Path(__file__).resolve().parents[1]


class Task0140QualificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_tool_exception_does_not_leak_secret_to_server_logs(self) -> None:
        sentinel = "TASK0140_" + "SECRET_SENTINEL"
        with patch.object(server, "execute_terminal", side_effect=RuntimeError(sentinel)):
            with self.assertLogs("mcp.server.mcpserver.server", level="ERROR") as captured:
                async with Client(server.mcp) as client:
                    result = await client.call_tool(
                        "terminal_exec",
                        {
                            "argv": ["/usr/bin/true"],
                            "cwd": str(ROOT),
                            "timeout_seconds": 5,
                        },
                    )

        self.assertTrue(result.is_error)
        serialized = result.model_dump_json(by_alias=True)
        self.assertNotIn(sentinel, serialized)
        self.assertFalse(
            any(sentinel in line for line in captured.output),
            "unexpected exception traceback leaked sentinel to MCP server logs",
        )


if __name__ == "__main__":
    unittest.main()
