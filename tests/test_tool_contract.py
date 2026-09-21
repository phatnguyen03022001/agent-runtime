from __future__ import annotations

import datetime
import unittest
from dataclasses import FrozenInstanceError
from enum import Enum

from agent_runtime import server
from agent_runtime.fs_read import (
    BATCH_OUTPUT_LIMIT_BYTES,
    BATCH_SCAN_LIMIT_BYTES,
    FS_READ_BATCH_CONTRACT,
    ITEM_OUTPUT_LIMIT_BYTES,
    ITEM_SCAN_LIMIT_BYTES,
)
from agent_runtime.tool_contract import (
    Authority,
    ContractError,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ReceiptV1,
    ToolAnnotations,
    ToolClass,
    ToolContract,
    canonical_structured_bytes,
    frame_bytes,
    make_receipt_v1,
)


class ToolContractKernelTests(unittest.TestCase):
    def test_exact_enum_sets(self) -> None:
        self.assertEqual({item.value for item in ToolClass}, {"read", "write", "process", "repo", "host"})
        self.assertEqual({item.value for item in NetworkAuthority}, {"none", "bounded"})
        self.assertEqual({item.value for item in MutationAuthority}, {"none", "bounded", "destructive"})
        self.assertEqual(
            {item.value for item in ContractErrorCode},
            {
                "INVALID_ARGUMENT",
                "OUTSIDE_WORKSPACE",
                "PRECONDITION_FAILED",
                "STATE_CHANGED",
                "CONFLICT",
                "LIMIT_EXCEEDED",
                "TIMEOUT",
                "PERMISSION_DENIED",
                "NOT_FOUND",
                "UNAVAILABLE",
                "INTERNAL_ERROR",
            },
        )

    def test_metadata_is_frozen_and_type_validated(self) -> None:
        authority = Authority(True, NetworkAuthority.NONE, MutationAuthority.NONE)
        annotations = ToolAnnotations(True, False, True, False)
        error = ContractError(ContractErrorCode.NOT_FOUND, "not_found", False)
        contract = ToolContract(
            name="x",
            tool_class=ToolClass.READ,
            authority=authority,
            annotations=annotations,
            preconditions={},
            bounds={},
            postconditions={},
        )
        with self.assertRaises(FrozenInstanceError):
            authority.workspace_bound = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            annotations.read_only = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            error.retryable = True  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            contract.name = "y"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            Authority(1, NetworkAuthority.NONE, MutationAuthority.NONE)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ToolAnnotations(1, False, True, False)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ContractError("NOT_FOUND", "not_found", False)  # type: ignore[arg-type]

    def test_stable_nonempty_strings_reject_empty_values(self) -> None:
        with self.assertRaises(ValueError):
            ContractError(ContractErrorCode.INTERNAL_ERROR, "", False)
        with self.assertRaises(ValueError):
            ToolContract(
                name="",
                tool_class=ToolClass.READ,
                authority=Authority(True, NetworkAuthority.NONE, MutationAuthority.NONE),
                annotations=ToolAnnotations(True, False, True, False),
                preconditions={},
                bounds={},
                postconditions={},
            )
        with self.assertRaises(ValueError):
            make_receipt_v1(kind="", subject={}, semantic_parameters={}, observed_state_bytes=b"")

    def test_receipt_v1_validates_schema_kind_and_digest(self) -> None:
        valid = ReceiptV1(schema_version=1, kind="read", digest="a" * 64)
        self.assertEqual(valid.schema_version, 1)
        with self.assertRaises(ValueError):
            ReceiptV1(schema_version=2, kind="read", digest="a" * 64)
        with self.assertRaises(ValueError):
            ReceiptV1(schema_version=1, kind="", digest="a" * 64)
        with self.assertRaises(ValueError):
            ReceiptV1(schema_version=1, kind="read", digest="A" * 64)
        with self.assertRaises(ValueError):
            ReceiptV1(schema_version=1, kind="read", digest="a" * 63)

    def test_dict_insertion_order_does_not_affect_canonical_bytes(self) -> None:
        first = {"b": 2, "a": 1}
        second = {"a": 1, "b": 2}
        self.assertEqual(canonical_structured_bytes(first), canonical_structured_bytes(second))

    def test_recursive_object_sorting_is_deterministic(self) -> None:
        value = {"z": 0, "a": {"c": 2, "b": 1}}
        self.assertEqual(canonical_structured_bytes(value), b'{"a":{"b":1,"c":2},"z":0}')

    def test_list_order_remains_semantic(self) -> None:
        self.assertNotEqual(canonical_structured_bytes([1, 2]), canonical_structured_bytes([2, 1]))

    def test_bool_and_int_keep_distinct_structured_semantics(self) -> None:
        self.assertEqual(canonical_structured_bytes(True), b"true")
        self.assertEqual(canonical_structured_bytes(1), b"1")
        self.assertNotEqual(canonical_structured_bytes(True), canonical_structured_bytes(1))

    def test_unsupported_structured_values_are_rejected(self) -> None:
        class StringEnum(str, Enum):
            VALUE = "value"

        for value in (
            1.0,
            b"bytes",
            ("tuple",),
            {"set"},
            datetime.datetime(2026, 1, 1),
            StringEnum.VALUE,
            object(),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    canonical_structured_bytes(value)

    def test_non_string_object_key_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            canonical_structured_bytes({1: "value"})

    def test_invalid_surrogate_string_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_structured_bytes("\ud800")
        with self.assertRaises(ValueError):
            canonical_structured_bytes({"\ud800": "value"})

    def test_bytes_inside_structured_values_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            canonical_structured_bytes({"payload": [b"bytes"]})

    def test_frame_uses_unsigned_uint64_big_endian_byte_length(self) -> None:
        self.assertEqual(frame_bytes(b"abc"), b"\x00\x00\x00\x00\x00\x00\x00\x03abc")
        with self.assertRaises(TypeError):
            frame_bytes(bytearray(b"abc"))  # type: ignore[arg-type]

    def test_framing_prevents_concatenation_ambiguity(self) -> None:
        self.assertNotEqual(
            frame_bytes(b"a") + frame_bytes(b"bc"),
            frame_bytes(b"ab") + frame_bytes(b"c"),
        )

    def _receipt(
        self,
        *,
        kind: str = "fs-read",
        subject: object | None = None,
        semantic_parameters: object | None = None,
        observed_state_bytes: bytes = b"state",
    ) -> ReceiptV1:
        return make_receipt_v1(
            kind=kind,
            subject={"path": "a.txt"} if subject is None else subject,
            semantic_parameters={"start": 1} if semantic_parameters is None else semantic_parameters,
            observed_state_bytes=observed_state_bytes,
        )

    def test_identical_semantic_receipt_input_is_deterministic(self) -> None:
        first = self._receipt(
            subject={"path": "a.txt", "meta": {"b": 2, "a": 1}},
            semantic_parameters={"lines": [1, 2]},
        )
        second = self._receipt(
            subject={"meta": {"a": 1, "b": 2}, "path": "a.txt"},
            semantic_parameters={"lines": [1, 2]},
        )
        self.assertEqual(first, second)
        self.assertRegex(first.digest, r"^[0-9a-f]{64}$")
        self.assertEqual(first.schema_version, 1)

    def test_changing_kind_changes_receipt_digest(self) -> None:
        self.assertNotEqual(self._receipt().digest, self._receipt(kind="other").digest)

    def test_changing_subject_changes_receipt_digest(self) -> None:
        self.assertNotEqual(self._receipt().digest, self._receipt(subject={"path": "b.txt"}).digest)

    def test_changing_semantic_parameters_changes_receipt_digest(self) -> None:
        self.assertNotEqual(
            self._receipt().digest,
            self._receipt(semantic_parameters={"start": 2}).digest,
        )

    def test_changing_observed_bytes_changes_receipt_digest(self) -> None:
        self.assertNotEqual(
            self._receipt().digest,
            self._receipt(observed_state_bytes=b"state-2").digest,
        )

    def test_observed_state_requires_exact_bytes(self) -> None:
        with self.assertRaises(TypeError):
            make_receipt_v1(
                kind="read",
                subject={},
                semantic_parameters={},
                observed_state_bytes=bytearray(b"state"),  # type: ignore[arg-type]
            )


class FsReadBatchContractAdoptionTests(unittest.IsolatedAsyncioTestCase):
    def test_contract_exact_authority_annotations_and_bounds(self) -> None:
        contract = FS_READ_BATCH_CONTRACT
        self.assertEqual(contract.name, "fs_read_batch")
        self.assertIs(contract.tool_class, ToolClass.READ)
        self.assertEqual(
            contract.authority,
            Authority(
                workspace_bound=True,
                network=NetworkAuthority.NONE,
                mutation=MutationAuthority.NONE,
            ),
        )
        self.assertEqual(
            contract.annotations,
            ToolAnnotations(
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=False,
            ),
        )
        self.assertEqual(contract.bounds["max_items"], 20)  # type: ignore[index]
        self.assertEqual(contract.bounds["item_output_bytes"], ITEM_OUTPUT_LIMIT_BYTES)  # type: ignore[index]
        self.assertEqual(contract.bounds["batch_output_bytes"], BATCH_OUTPUT_LIMIT_BYTES)  # type: ignore[index]
        self.assertEqual(contract.bounds["item_scan_bytes"], ITEM_SCAN_LIMIT_BYTES)  # type: ignore[index]
        self.assertEqual(contract.bounds["batch_scan_bytes"], BATCH_SCAN_LIMIT_BYTES)  # type: ignore[index]
        self.assertEqual(contract.preconditions["path"]["kind"], "cwd-relative-descendant")  # type: ignore[index]
        self.assertEqual(contract.preconditions["path"]["disallowed_components"], ["", ".", ".."])  # type: ignore[index]
        self.assertIs(contract.preconditions["path"]["symlink_traversal"], False)  # type: ignore[index]
        self.assertIs(contract.preconditions["path"]["regular_files_only"], True)  # type: ignore[index]
        self.assertEqual(contract.postconditions["content_encoding"], "utf-8-strict")  # type: ignore[index]
        self.assertEqual(contract.postconditions["result_order"], "request-order")  # type: ignore[index]
        self.assertEqual(contract.postconditions["filesystem_failures"], "per-item")  # type: ignore[index]

    async def test_public_surface_annotations_and_fs_read_schemas_remain_unchanged(self) -> None:
        listed = await server.mcp.list_tools()
        self.assertEqual(
            tuple(tool.name for tool in listed),
            (
                "terminal_exec",
                "terminal_start",
                "terminal_poll",
                "terminal_control",
                "terminal_resize",
                "capacity_observer",
                "fs_read_batch",
                "repo_observer",
                "repo_fast_forward",
                "repo_publish",
                "screen_capture",
            ),
        )
        tools = {tool.name: tool for tool in listed}
        tool = tools["fs_read_batch"]
        annotations = tool.annotations.model_dump(by_alias=True)
        self.assertEqual(
            (
                annotations["readOnlyHint"],
                annotations["destructiveHint"],
                annotations["idempotentHint"],
                annotations["openWorldHint"],
            ),
            (
                FS_READ_BATCH_CONTRACT.annotations.read_only,
                FS_READ_BATCH_CONTRACT.annotations.destructive,
                FS_READ_BATCH_CONTRACT.annotations.idempotent,
                FS_READ_BATCH_CONTRACT.annotations.open_world,
            ),
        )

        input_schema = tool.input_schema
        self.assertEqual(set(input_schema["properties"]), {"cwd", "items"})
        self.assertEqual(input_schema["properties"]["items"]["minItems"], 1)
        self.assertEqual(input_schema["properties"]["items"]["maxItems"], 20)
        item = input_schema["$defs"]["FsReadItem"]
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(set(item["properties"]), {"path", "start_line", "end_line"})

        output = tool.output_schema
        self.assertIs(output["additionalProperties"], False)
        self.assertEqual(set(output["properties"]), {"items"})
        ok = output["$defs"]["FsReadOkResult"]
        error = output["$defs"]["FsReadErrorResult"]
        self.assertEqual(set(ok["properties"]), {"status", "path", "start_line", "end_line", "text"})
        self.assertEqual(
            set(error["properties"]),
            {"status", "path", "start_line", "end_line", "error_code", "message"},
        )
        self.assertEqual(
            set(error["properties"]["error_code"]["enum"]),
            {
                "NOT_FOUND",
                "ACCESS_DENIED",
                "SYMLINK_DISALLOWED",
                "NOT_REGULAR_FILE",
                "INVALID_UTF8",
                "ITEM_OUTPUT_LIMIT_EXCEEDED",
                "BATCH_OUTPUT_LIMIT_EXCEEDED",
                "ITEM_SCAN_LIMIT_EXCEEDED",
                "BATCH_SCAN_LIMIT_EXCEEDED",
                "READ_FAILED",
            },
        )
        self.assertNotIn("receipt", output["properties"])


if __name__ == "__main__":
    unittest.main()
