from __future__ import annotations

import plistlib
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from agent_runtime import server
from agent_runtime.capability_registry import (
    CAPABILITY_NAMES,
    CAPABILITY_REGISTRY,
    TOOL_CONTRACT_KERNEL_VERSION,
    capability_descriptors,
)
from agent_runtime.contracts import CapabilityDescriptor
from agent_runtime.tool_contract import ToolContract
from agent_runtime.version import RUNTIME_VERSION

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAMES = (
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
DESCRIPTOR_FIELDS = {
    "schema_version",
    "runtime_version",
    "tool_contract_kernel_version",
    "name",
    "tool_contract_version",
    "lifecycle",
    "authority",
    "annotations",
    "request_schema_version",
    "result_schema_version",
    "bounds",
    "supported",
    "available",
    "unavailable_reason_code",
}


def _annotation_tuple(tool: object) -> tuple[bool, bool, bool, bool]:
    values = tool.annotations.model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


class CapabilityRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_contract_kernel_shape_is_unchanged(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(ToolContract)),
            (
                "name",
                "tool_class",
                "authority",
                "annotations",
                "preconditions",
                "bounds",
                "postconditions",
            ),
        )

    def test_registry_is_exact_ordered_unique_19_tool_inventory(self) -> None:
        self.assertEqual(CAPABILITY_NAMES, EXPECTED_NAMES)
        self.assertEqual(len(CAPABILITY_REGISTRY), 19)
        self.assertEqual(len({id(binding.contract) for binding in CAPABILITY_REGISTRY}), 19)
        self.assertTrue(all(isinstance(binding.contract, ToolContract) for binding in CAPABILITY_REGISTRY))

    def test_descriptor_schema_and_contract_projection_are_exact(self) -> None:
        self.assertEqual(set(CapabilityDescriptor.model_fields), DESCRIPTOR_FIELDS)
        descriptors = capability_descriptors()
        self.assertEqual(tuple(item.name for item in descriptors), EXPECTED_NAMES)
        for binding, descriptor in zip(CAPABILITY_REGISTRY, descriptors, strict=True):
            contract = binding.contract
            self.assertEqual(descriptor.schema_version, 1)
            self.assertEqual(descriptor.runtime_version, RUNTIME_VERSION)
            self.assertEqual(descriptor.tool_contract_kernel_version, TOOL_CONTRACT_KERNEL_VERSION)
            self.assertEqual(descriptor.tool_contract_version, 1)
            self.assertEqual(descriptor.lifecycle, "stable")
            self.assertEqual(descriptor.authority.workspace_bound, contract.authority.workspace_bound)
            self.assertEqual(descriptor.authority.network, contract.authority.network.value)
            self.assertEqual(descriptor.authority.mutation, contract.authority.mutation.value)
            self.assertEqual(
                (
                    descriptor.annotations.read_only,
                    descriptor.annotations.destructive,
                    descriptor.annotations.idempotent,
                    descriptor.annotations.open_world,
                ),
                (
                    contract.annotations.read_only,
                    contract.annotations.destructive,
                    contract.annotations.idempotent,
                    contract.annotations.open_world,
                ),
            )
            self.assertEqual(descriptor.bounds, contract.bounds)
            expected_schema_version = 2 if descriptor.name in {"terminal_start", "terminal_poll"} else 1
            self.assertEqual(descriptor.request_schema_version, expected_schema_version)
            self.assertTrue(descriptor.supported)

        screen = descriptors[EXPECTED_NAMES.index("screen_capture")]
        self.assertFalse(screen.available)
        self.assertEqual(screen.unavailable_reason_code, "VISUAL_PERCEPTION_BLOCKED")
        self.assertIsNone(screen.result_schema_version)
        for descriptor in descriptors:
            if descriptor.name == "screen_capture":
                continue
            self.assertTrue(descriptor.available)
            self.assertIsNone(descriptor.unavailable_reason_code)
            expected_schema_version = 2 if descriptor.name in {"terminal_start", "terminal_poll"} else 1
            self.assertEqual(descriptor.result_schema_version, expected_schema_version)

    async def test_registered_mcp_surface_is_registry_order_and_contract_annotated(self) -> None:
        tools = await server.mcp.list_tools()
        self.assertEqual(tuple(tool.name for tool in tools), EXPECTED_NAMES)
        self.assertEqual(server.PUBLIC_TOOL_NAMES, CAPABILITY_NAMES)
        for binding, tool in zip(CAPABILITY_REGISTRY, tools, strict=True):
            annotations = binding.contract.annotations
            self.assertEqual(
                _annotation_tuple(tool),
                (
                    annotations.read_only,
                    annotations.destructive,
                    annotations.idempotent,
                    annotations.open_world,
                ),
            )

    async def test_runtime_capabilities_is_static_deterministic_and_probe_free(self) -> None:
        blockers = (
            patch.object(server, "execute_terminal", side_effect=AssertionError("process probe")),
            patch.object(server, "observe_capacity", side_effect=AssertionError("host probe")),
            patch.object(server, "observe_repository", side_effect=AssertionError("repo probe")),
            patch.object(server, "fast_forward_repository", side_effect=AssertionError("repo mutation")),
            patch.object(server, "publish_repository", side_effect=AssertionError("network mutation")),
            patch.object(server, "capture_screen", side_effect=AssertionError("capture probe")),
        )
        for blocker in blockers:
            blocker.start()
            self.addCleanup(blocker.stop)

        first = await server.mcp.call_tool("runtime_capabilities", {})
        second = await server.mcp.call_tool("runtime_capabilities", {})
        self.assertFalse(first.is_error, first)
        self.assertFalse(second.is_error, second)
        self.assertEqual(first.structured_content, second.structured_content)
        payload = first.structured_content
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["runtime_version"], RUNTIME_VERSION)
        self.assertEqual(payload["tool_contract_kernel_version"], 1)
        self.assertEqual(
            tuple(item["name"] for item in payload["capabilities"]),
            EXPECTED_NAMES,
        )

    def test_runtime_version_has_one_python_ssot_and_validated_package_projection(self) -> None:
        self.assertEqual(RUNTIME_VERSION, "0.2.1")
        self.assertEqual(server.mcp.version, RUNTIME_VERSION)
        literal_sources = [
            path.name
            for path in sorted((ROOT / "agent_runtime").glob("*.py"))
            if '"0.2.1"' in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(literal_sources, ["version.py"])

        plist = plistlib.loads((ROOT / "macos/AppBundle/Info.plist").read_bytes())
        self.assertEqual(plist["CFBundleShortVersionString"], RUNTIME_VERSION)
        package_script = (ROOT / "macos/package_app.sh").read_text(encoding="utf-8")
        self.assertIn('RUNTIME_VERSION="$("$PYTHON_BIN" "$SOURCE_ROOT/agent_runtime/version.py")"', package_script)
        self.assertIn('[[ "$PLIST_RUNTIME_VERSION" == "$RUNTIME_VERSION" ]]', package_script)


if __name__ == "__main__":
    unittest.main()
