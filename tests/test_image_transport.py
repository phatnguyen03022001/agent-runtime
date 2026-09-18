from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import ImageContent, TextContent

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "image_transport_probe.py"
FIXTURE_DIR = ROOT / "tests" / "fixtures"
FIXTURES = {
    "tiny": FIXTURE_DIR / "task0087-tiny.png",
    "medium": FIXTURE_DIR / "task0087-medium.png",
    "visual": FIXTURE_DIR / "task0087-visual.png",
}


def _expected_visual_token() -> str:
    return hashlib.sha256(b"TASK-0087-VISUAL-TOKEN-v1").hexdigest()[:8].upper()


def _stdio_parameters() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-u", str(PROBE)],
        cwd=str(ROOT),
        env={
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


def _image_blocks(result: object) -> list[ImageContent]:
    return [
        block
        for block in getattr(result, "content")
        if isinstance(block, ImageContent)
    ]


class ImageTransportWireProofTests(unittest.IsolatedAsyncioTestCase):
    def test_proof_artifacts_exist_and_are_png(self) -> None:
        self.assertTrue(PROBE.is_file(), "test-only image transport probe is missing")
        for label, path in FIXTURES.items():
            with self.subTest(label=label):
                data = path.read_bytes()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertGreater(len(data), 0)

    async def test_real_image_content_preserves_exact_bytes_and_sha(self) -> None:
        source = FIXTURES["tiny"].read_bytes()
        source_sha = hashlib.sha256(source).hexdigest()

        async with Client(_stdio_parameters(), read_timeout_seconds=10) as client:
            result = await client.call_tool("image_transport_probe", {"fixture": "tiny"})

        self.assertFalse(result.is_error)
        images = _image_blocks(result)
        self.assertEqual(len(images), 1)
        image = images[0]
        self.assertEqual(image.type, "image")
        self.assertEqual(image.mime_type, "image/png")

        decoded = base64.b64decode(image.data, validate=True)
        self.assertEqual(decoded, source)
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), source_sha)

    async def test_image_and_structured_content_survive_same_call(self) -> None:
        source = FIXTURES["medium"].read_bytes()

        async with Client(_stdio_parameters(), read_timeout_seconds=10) as client:
            result = await client.call_tool("image_transport_probe", {"fixture": "medium"})

        self.assertFalse(result.is_error)
        images = _image_blocks(result)
        self.assertEqual(len(images), 1)
        decoded = base64.b64decode(images[0].data, validate=True)
        self.assertEqual(decoded, source)
        self.assertEqual(
            result.structured_content,
            {
                "fixture": "medium",
                "mime_type": "image/png",
                "raw_bytes": len(source),
                "sha256": hashlib.sha256(source).hexdigest(),
            },
        )

    async def test_visual_fixture_token_does_not_leak_through_text_or_metadata(self) -> None:
        expected_token = _expected_visual_token()
        fixture = FIXTURES["visual"]
        source = fixture.read_bytes()

        self.assertNotIn(expected_token.lower(), fixture.name.lower())
        self.assertNotIn(expected_token.encode("ascii"), source)
        self.assertNotIn(expected_token, PROBE.read_text(encoding="utf-8"))

        async with Client(_stdio_parameters(), read_timeout_seconds=10) as client:
            listing = await client.list_tools()
            tool = next(tool for tool in listing.tools if tool.name == "image_transport_probe")
            result = await client.call_tool("image_transport_probe", {"fixture": "visual"})

        self.assertFalse(result.is_error)
        tool_surface = json.dumps(tool.model_dump(by_alias=True), sort_keys=True)
        self.assertNotIn(expected_token, tool_surface)
        text_blocks = [
            block.text
            for block in result.content
            if isinstance(block, TextContent)
        ]
        self.assertEqual(text_blocks, [])
        self.assertNotIn(
            expected_token,
            json.dumps(result.structured_content, sort_keys=True),
        )
        decoded = base64.b64decode(_image_blocks(result)[0].data, validate=True)
        self.assertEqual(decoded, source)

    async def test_bounded_payload_matrix_records_positive_proof(self) -> None:
        rows: list[dict[str, object]] = []

        async with Client(_stdio_parameters(), read_timeout_seconds=10) as client:
            for label in ("tiny", "medium"):
                source = FIXTURES[label].read_bytes()
                result = await client.call_tool(
                    "image_transport_probe",
                    {"fixture": label},
                )
                self.assertFalse(result.is_error, label)
                image = _image_blocks(result)[0]
                decoded = base64.b64decode(image.data, validate=True)
                self.assertEqual(decoded, source, label)
                rows.append(
                    {
                        "fixture": label,
                        "raw_png_bytes": len(source),
                        "base64_chars": len(image.data),
                        "content_blocks": len(result.content),
                        "has_image_content": True,
                        "has_structured_content": result.structured_content is not None,
                        "sha256": hashlib.sha256(source).hexdigest(),
                        "status": "PASS",
                    }
                )

        evidence_path = os.environ.get("TASK0087_PAYLOAD_EVIDENCE")
        if evidence_path:
            Path(evidence_path).write_text(
                json.dumps(
                    {
                        "maximum": "MAXIMUM_NOT_CLAIMED",
                        "largest_positive_raw_png_bytes": max(
                            int(row["raw_png_bytes"]) for row in rows
                        ),
                        "fixtures": rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )


if __name__ == "__main__":
    unittest.main()
