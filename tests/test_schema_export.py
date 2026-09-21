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
    def test_bundle_projects_actual_registered_schemas_and_exact_contracts(self) -> None:
        bundle = asyncio.run(build_schema_bundle())
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual(bundle["schema_version"], 1)
        self.assertEqual(bundle["runtime_version"], RUNTIME_VERSION)
        self.assertEqual(bundle["tool_contract_kernel_version"], 1)
        entries = bundle["capabilities"]
        self.assertEqual(tuple(entry["descriptor"]["name"] for entry in entries), CAPABILITY_NAMES)
        self.assertEqual(len(entries), len(tools))

        for binding, tool, entry in zip(CAPABILITY_REGISTRY, tools, entries, strict=True):
            self.assertEqual(entry["descriptor"], descriptor_for(binding).model_dump(mode="json"))
            self.assertEqual(entry["tool_contract"], tool_contract_projection(binding.contract))
            self.assertEqual(entry["request_schema"], tool.input_schema)
            self.assertEqual(entry["result_schema"], tool.output_schema)

        resultless = [
            entry["descriptor"]["name"]
            for entry in entries
            if entry["result_schema"] is None
        ]
        self.assertEqual(resultless, ["screen_capture"])

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
