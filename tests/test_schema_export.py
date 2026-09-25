from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from agent_runtime import server
from agent_runtime.capability_registry import (
    ADVERTISED_TOOL_NAMES,
    CAPABILITY_NAMES,
    CAPABILITY_REGISTRY,
    descriptor_for,
    tool_contract_projection,
)
from agent_runtime.schema_export import build_schema_bundle, export_schema_bytes
from agent_runtime.tool_contract import canonical_structured_bytes
from agent_runtime.version import RUNTIME_VERSION

ROOT = Path(__file__).resolve().parents[1]


class SchemaExportTests(unittest.TestCase):
    def test_bundle_projects_known_inventory_and_only_advertised_registered_schemas(self) -> None:
        bundle = asyncio.run(build_schema_bundle())
        tools = asyncio.run(server.mcp.list_tools())
        tools_by_name = {tool.name: tool for tool in tools}

        self.assertEqual(bundle["schema_version"], 1)
        self.assertEqual(bundle["runtime_version"], RUNTIME_VERSION)
        self.assertEqual(bundle["tool_contract_kernel_version"], 2)
        self.assertEqual(tuple(tools_by_name), ADVERTISED_TOOL_NAMES)

        entries = bundle["capabilities"]
        self.assertEqual(
            tuple(entry["descriptor"]["name"] for entry in entries),
            CAPABILITY_NAMES,
        )
        self.assertEqual(len(entries), 21)
        self.assertEqual(len(tools), 20)

        for binding, entry in zip(CAPABILITY_REGISTRY, entries, strict=True):
            name = binding.contract.name
            self.assertEqual(entry["descriptor"], descriptor_for(binding).model_dump(mode="json"))
            self.assertEqual(entry["tool_contract"], tool_contract_projection(binding.contract))
            if binding.advertised:
                tool = tools_by_name[name]
                self.assertEqual(entry["request_schema"], tool.input_schema)
                self.assertEqual(entry["result_schema"], tool.output_schema)
            else:
                self.assertEqual(name, "screen_capture")
                self.assertIsNone(entry["request_schema"])
                self.assertIsNone(entry["result_schema"])

        hidden = [
            entry["descriptor"]["name"]
            for entry in entries
            if not entry["descriptor"]["advertised"]
        ]
        self.assertEqual(hidden, ["screen_capture"])

    def test_export_bytes_are_canonical_repeatable_and_digest_bound(self) -> None:
        first = export_schema_bytes()
        second = export_schema_bytes()
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertFalse(first.endswith(b"\n\n"))
        parsed = json.loads(first)
        digest = parsed.pop("bundle_sha256")
        self.assertEqual(
            digest,
            hashlib.sha256(canonical_structured_bytes(parsed)).hexdigest(),
        )
        self.assertEqual(first, canonical_structured_bytes({**parsed, "bundle_sha256": digest}) + b"\n")

    def test_module_cli_is_environment_independent_for_irrelevant_runtime_state(self) -> None:
        def run(home: str, workspace: str) -> bytes:
            env = os.environ.copy()
            env["HOME"] = home
            env["AGENT_RUNTIME_WORKSPACE_ROOT"] = workspace
            completed = subprocess.run(
                [sys.executable, "-m", "agent_runtime.schema_export"],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(completed.stderr, b"")
            return completed.stdout

        first = run("/tmp/agent-runtime-schema-home-a", "/tmp/agent-runtime-schema-workspace-a")
        second = run("/tmp/agent-runtime-schema-home-b", "/tmp/agent-runtime-schema-workspace-b")
        self.assertEqual(first, second)
        self.assertNotIn(str(ROOT).encode(), first)
        self.assertNotIn(str(Path.home()).encode(), first)


if __name__ == "__main__":
    unittest.main()
