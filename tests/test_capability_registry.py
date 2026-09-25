from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from agent_runtime import server
from agent_runtime.capability_registry import (
    ADVERTISED_TOOL_NAMES,
    CAPABILITY_NAMES,
    CAPABILITY_REGISTRY,
    TOOL_CONTRACT_KERNEL_VERSION,
    capability_descriptors,
)
from agent_runtime.capacity import heavy_execution_admission
from agent_runtime.contracts import CapabilityDescriptor
from agent_runtime.session import MAX_WAIT_MS, RUNNING_HARD_WALL_SECONDS, configured_session_limit
from agent_runtime.tool_contract import ToolContract, canonical_structured_bytes
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
    "fs_manage",
    "repo_observer",
    "repo_remote_observer",
    "repo_diff",
    "repo_stage",
    "repo_commit",
    "repo_fast_forward",
    "repo_publish",
    "screen_capture",
    "runtime_capabilities",
)
EXPECTED_ADVERTISED = tuple(name for name in EXPECTED_NAMES if name != "screen_capture")
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
    "advertised",
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

    def test_registry_is_exact_ordered_unique_21_capability_inventory(self) -> None:
        self.assertEqual(CAPABILITY_NAMES, EXPECTED_NAMES)
        self.assertEqual(ADVERTISED_TOOL_NAMES, EXPECTED_ADVERTISED)
        self.assertEqual(len(CAPABILITY_REGISTRY), 21)
        self.assertEqual(len(ADVERTISED_TOOL_NAMES), 20)
        self.assertEqual(len({id(binding.contract) for binding in CAPABILITY_REGISTRY}), 21)
        self.assertTrue(all(isinstance(binding.contract, ToolContract) for binding in CAPABILITY_REGISTRY))

    def test_descriptor_schema_and_advertisement_are_exact(self) -> None:
        self.assertEqual(set(CapabilityDescriptor.model_fields), DESCRIPTOR_FIELDS)
        descriptors = capability_descriptors()
        self.assertEqual(tuple(item.name for item in descriptors), EXPECTED_NAMES)

        for binding, descriptor in zip(CAPABILITY_REGISTRY, descriptors, strict=True):
            contract = binding.contract
            self.assertEqual(descriptor.schema_version, 2)
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
            self.assertEqual(descriptor.advertised, binding.advertised)
            expected_request_version = {
                "terminal_exec": 2,
                "terminal_start": 4,
                "terminal_poll": 4,
                "fs_list": 2,
                "fs_search": 2,
                "repo_observer": 2,
                "repo_diff": 2,
                "runtime_capabilities": 2,
            }.get(descriptor.name, 1)
            self.assertEqual(descriptor.request_schema_version, expected_request_version)
            self.assertTrue(descriptor.supported)

        screen = descriptors[EXPECTED_NAMES.index("screen_capture")]
        self.assertFalse(screen.available)
        self.assertFalse(screen.advertised)
        self.assertEqual(screen.unavailable_reason_code, "VISUAL_PERCEPTION_BLOCKED")
        self.assertIsNone(screen.result_schema_version)

        for descriptor in descriptors:
            if descriptor.name == "screen_capture":
                continue
            self.assertTrue(descriptor.available)
            self.assertTrue(descriptor.advertised)
            self.assertIsNone(descriptor.unavailable_reason_code)
            expected_result_version = {
                "terminal_exec": 2,
                "terminal_start": 4,
                "terminal_poll": 4,
                "capacity_observer": 2,
                "fs_read_batch": 2,
                "fs_list": 2,
                "fs_search": 2,
                "repo_observer": 2,
                "repo_diff": 2,
                "runtime_capabilities": 2,
            }.get(descriptor.name, 1)
            self.assertEqual(descriptor.result_schema_version, expected_result_version)

    async def test_registered_mcp_surface_is_exact_advertised_registry_order(self) -> None:
        tools = await server.mcp.list_tools()
        self.assertEqual(tuple(tool.name for tool in tools), EXPECTED_ADVERTISED)
        self.assertEqual(server.PUBLIC_TOOL_NAMES, ADVERTISED_TOOL_NAMES)
        advertised_bindings = tuple(binding for binding in CAPABILITY_REGISTRY if binding.advertised)
        for binding, tool in zip(advertised_bindings, tools, strict=True):
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

    async def test_runtime_capabilities_summary_is_compact_deterministic_and_probe_free(self) -> None:
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
        second = await server.mcp.call_tool("runtime_capabilities", {"detail": "summary"})
        self.assertFalse(first.is_error, first)
        self.assertFalse(second.is_error, second)
        self.assertEqual(first.structured_content, second.structured_content)

        payload = first.structured_content
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "detail",
                "runtime_version",
                "runtime_revision",
                "tool_contract_kernel_version",
                "advertised_tool_count",
                "capability_count",
                "available_count",
                "unavailable_count",
                "execution",
            },
        )
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["detail"], "summary")
        self.assertEqual(payload["runtime_version"], RUNTIME_VERSION)
        self.assertIsNone(payload["runtime_revision"])
        self.assertEqual(payload["tool_contract_kernel_version"], 2)
        self.assertEqual(payload["advertised_tool_count"], 20)
        self.assertEqual(payload["capability_count"], 21)
        self.assertEqual(payload["available_count"], 20)
        self.assertEqual(payload["unavailable_count"], 1)
        self.assertEqual(
            payload["execution"],
            {
                "heavy_ceiling": heavy_execution_admission().limit,
                "active_session_ceiling": configured_session_limit(),
                "terminal_poll_max_wait_ms": MAX_WAIT_MS,
                "running_hard_wall_ms": int(RUNNING_HARD_WALL_SECONDS * 1000),
            },
        )
        self.assertLessEqual(
            len(canonical_structured_bytes(payload)),
            1024,
        )

    async def test_runtime_capabilities_full_filter_preserves_registry_order_and_global_counts(self) -> None:
        full = await server.mcp.call_tool("runtime_capabilities", {"detail": "full"})
        self.assertFalse(full.is_error, full)
        self.assertEqual(full.structured_content["detail"], "full")
        self.assertEqual(
            tuple(item["name"] for item in full.structured_content["capabilities"]),
            EXPECTED_NAMES,
        )

        filtered = await server.mcp.call_tool(
            "runtime_capabilities",
            {
                "detail": "full",
                "names": ["screen_capture", "terminal_exec"],
            },
        )
        self.assertFalse(filtered.is_error, filtered)
        payload = filtered.structured_content
        self.assertEqual(
            tuple(item["name"] for item in payload["capabilities"]),
            ("terminal_exec", "screen_capture"),
        )
        self.assertEqual(payload["advertised_tool_count"], 20)
        self.assertEqual(payload["capability_count"], 21)
        self.assertEqual(payload["available_count"], 20)
        self.assertEqual(payload["unavailable_count"], 1)
        screen = payload["capabilities"][1]
        self.assertEqual(screen["schema_version"], 2)
        self.assertTrue(screen["supported"])
        self.assertFalse(screen["available"])
        self.assertFalse(screen["advertised"])
        self.assertEqual(screen["unavailable_reason_code"], "VISUAL_PERCEPTION_BLOCKED")

    async def test_runtime_capabilities_invalid_filters_fail_closed(self) -> None:
        cases = (
            {"detail": "summary", "names": ["terminal_exec"]},
            {"detail": "full", "names": ["terminal_exec", "terminal_exec"]},
            {"detail": "full", "names": ["does_not_exist"]},
            {"detail": "full", "names": []},
            {"detail": "full", "names": [""]},
            {"detail": "unknown"},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = await server.mcp.call_tool("runtime_capabilities", arguments)
                self.assertTrue(result.is_error, result)
                self.assertEqual(result.structured_content["error"]["code"], "INVALID_ARGUMENT")
                self.assertEqual(result.structured_content["error"]["effect_state"], "absent")

    def test_runtime_revision_uses_validated_reserved_process_identity(self) -> None:
        script = (
            "from agent_runtime.capability_registry import RUNTIME_REVISION; "
            "print(RUNTIME_REVISION if RUNTIME_REVISION is not None else 'null')"
        )
        valid_env = os.environ.copy()
        valid_env["AGENT_RUNTIME_REVISION"] = "a" * 40
        valid = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=valid_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(valid.stdout.strip(), "a" * 40)

        invalid_env = os.environ.copy()
        invalid_env["AGENT_RUNTIME_REVISION"] = "A" * 40
        invalid = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=invalid_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("exact lowercase 40-hex", invalid.stderr)

    def test_runtime_version_has_one_python_ssot_and_validated_package_projection(self) -> None:
        self.assertEqual(RUNTIME_VERSION, "0.4.0")
        self.assertEqual(server.mcp.version, RUNTIME_VERSION)
        literal_sources = [
            path.name
            for path in sorted((ROOT / "agent_runtime").glob("*.py"))
            if '"0.4.0"' in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(literal_sources, ["version.py"])

        plist = plistlib.loads((ROOT / "macos/AppBundle/Info.plist").read_bytes())
        self.assertEqual(plist["CFBundleShortVersionString"], RUNTIME_VERSION)
        package_script = (ROOT / "macos/package_app.sh").read_text(encoding="utf-8")
        self.assertIn('RUNTIME_VERSION="$("$PYTHON_BIN" "$SOURCE_ROOT/agent_runtime/version.py")"', package_script)
        self.assertIn('[[ "$PLIST_RUNTIME_VERSION" == "$RUNTIME_VERSION" ]]', package_script)


if __name__ == "__main__":
    unittest.main()
