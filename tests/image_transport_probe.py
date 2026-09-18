from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Literal

try:
    from mcp.server import MCPServer
except ImportError:
    from mcp.server.mcpserver import MCPServer

from mcp.types import CallToolResult, ImageContent

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "tiny": ROOT / "tests" / "fixtures" / "task0087-tiny.png",
    "medium": ROOT / "tests" / "fixtures" / "task0087-medium.png",
    "visual": ROOT / "tests" / "fixtures" / "task0087-visual.png",
}

mcp = MCPServer(
    name="TASK-0087 isolated image transport proof",
    version="1",
    description="Test-only MCP server for deterministic PNG wire transport proof.",
)


@mcp.tool(
    name="image_transport_probe",
    description="Return one deterministic test PNG as image content with bounded metadata.",
)
def image_transport_probe(
    fixture: Literal["tiny", "medium", "visual"],
) -> CallToolResult:
    source = FIXTURES[fixture].read_bytes()
    return CallToolResult(
        content=[
            ImageContent(
                type="image",
                data=base64.b64encode(source).decode("ascii"),
                mimeType="image/png",
            )
        ],
        structuredContent={
            "fixture": fixture,
            "mime_type": "image/png",
            "raw_bytes": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),
        },
    )


if __name__ == "__main__":
    mcp.run()
