from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import fs_patch as fs_patch_module
from agent_runtime.contracts import CapabilityFailure, FsPatchEdit
from agent_runtime.fs_patch import (
    FS_PATCH_CONTRACT,
    MAX_EDITS,
    MAX_EDIT_BYTES,
    MAX_INPUT_FILE_BYTES,
    MAX_OUTPUT_FILE_BYTES,
    patch_file,
)
from agent_runtime.tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class FsPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.path = self.root / "target.txt"

    def _write(self, raw: bytes = b"alpha\nbeta\n", mode: int = 0o640) -> bytes:
        self.path.write_bytes(raw)
        os.chmod(self.path, mode)
        return raw

    def _edit(self, old: str, new: str) -> FsPatchEdit:
        return FsPatchEdit(old_text=old, new_text=new)

    def test_contract_exact_authority_annotations_and_bounds(self) -> None:
        self.assertEqual(FS_PATCH_CONTRACT.name, "fs_patch")
        self.assertIs(FS_PATCH_CONTRACT.tool_class, ToolClass.WRITE)
        self.assertEqual(
            FS_PATCH_CONTRACT.authority,
            Authority(True, NetworkAuthority.NONE, MutationAuthority.BOUNDED),
        )
        self.assertEqual(
            FS_PATCH_CONTRACT.annotations,
            ToolAnnotations(False, True, False, False),
        )
        self.assertEqual(MAX_INPUT_FILE_BYTES, 1024 * 1024)
        self.assertEqual(MAX_OUTPUT_FILE_BYTES, 1024 * 1024)
        self.assertEqual(MAX_EDITS, 20)
        self.assertEqual(MAX_EDIT_BYTES, 256 * 1024)

    def test_expected_sha_is_mandatory_exact_lowercase_state_guard(self) -> None:
        raw = self._write()
        with self.assertRaises(CapabilityFailure) as caught:
            patch_file(str(self.root), "target.txt", "0" * 64, [self._edit("alpha", "omega")])
        self.assertEqual(caught.exception.code, ContractErrorCode.PRECONDITION_FAILED)
        self.assertEqual(caught.exception.reason_code, "EXPECTED_SHA256_MISMATCH")
        self.assertEqual(self.path.read_bytes(), raw)

        with self.assertRaises(CapabilityFailure) as invalid:
            patch_file(str(self.root), "target.txt", _sha(raw).upper(), [self._edit("alpha", "omega")])
        self.assertEqual(invalid.exception.code, ContractErrorCode.INVALID_ARGUMENT)

    def test_sequential_exact_once_edits_use_evolving_text(self) -> None:
        raw = self._write(b"alpha beta\n")
        result = patch_file(
            str(self.root),
            "target.txt",
            _sha(raw),
            [self._edit("alpha", "gamma"), self._edit("gamma beta", "done")],
        )
        self.assertEqual(self.path.read_bytes(), b"done\n")
        self.assertEqual(result.edits_applied, 2)
        self.assertEqual(result.sha256_before, _sha(raw))
        self.assertEqual(result.sha256_after, _sha(b"done\n"))

    def test_old_text_not_found_and_not_unique_are_typed_and_non_mutating(self) -> None:
        raw = self._write(b"x x\n")
        with self.assertRaises(CapabilityFailure) as duplicate:
            patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("x", "y")])
        self.assertEqual(duplicate.exception.code, ContractErrorCode.CONFLICT)
        self.assertEqual(duplicate.exception.reason_code, "OLD_TEXT_NOT_UNIQUE")
        self.assertEqual(self.path.read_bytes(), raw)

        raw = self._write(b"alpha\n")
        with self.assertRaises(CapabilityFailure) as missing:
            patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("beta", "y")])
        self.assertEqual(missing.exception.code, ContractErrorCode.PRECONDITION_FAILED)
        self.assertEqual(missing.exception.reason_code, "OLD_TEXT_NOT_FOUND")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_overlapping_old_text_occurrences_are_conflict(self) -> None:
        raw = self._write(b"aaa")
        with self.assertRaises(CapabilityFailure) as caught:
            patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("aa", "b")])
        self.assertEqual(caught.exception.code, ContractErrorCode.CONFLICT)
        self.assertEqual(caught.exception.reason_code, "OLD_TEXT_NOT_UNIQUE")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_noop_edit_revalidates_but_does_not_replace_inode(self) -> None:
        raw = self._write(b"alpha\n")
        inode_before = self.path.stat().st_ino
        with patch(
            "agent_runtime.fs_patch.os.replace",
            side_effect=AssertionError("no-op edit must not replace target"),
        ):
            first = patch_file(
                str(self.root),
                "target.txt",
                _sha(raw),
                [self._edit("alpha", "alpha")],
            )
            second = patch_file(
                str(self.root),
                "target.txt",
                _sha(raw),
                [self._edit("alpha", "alpha")],
            )
        self.assertEqual(first.sha256_before, first.sha256_after)
        self.assertEqual(second.sha256_before, second.sha256_after)
        self.assertEqual(self.path.stat().st_ino, inode_before)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_replay_after_success_cannot_mutate_again(self) -> None:
        raw = self._write(b"alpha\n")
        patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("alpha", "beta")])
        after = self.path.read_bytes()
        with self.assertRaises(CapabilityFailure) as caught:
            patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("alpha", "beta")])
        self.assertEqual(caught.exception.reason_code, "EXPECTED_SHA256_MISMATCH")
        self.assertEqual(self.path.read_bytes(), after)

    def test_path_traversal_and_symlink_are_rejected(self) -> None:
        raw = self._write()
        with self.assertRaises(CapabilityFailure) as traversal:
            patch_file(str(self.root), "../target.txt", _sha(raw), [self._edit("alpha", "x")])
        self.assertEqual(traversal.exception.code, ContractErrorCode.INVALID_ARGUMENT)

        real = self.root / "real.txt"
        real.write_bytes(raw)
        link = self.root / "link.txt"
        link.symlink_to(real)
        with self.assertRaises(CapabilityFailure) as symlink:
            patch_file(str(self.root), "link.txt", _sha(raw), [self._edit("alpha", "x")])
        self.assertEqual(symlink.exception.reason_code, "SYMLINK_DISALLOWED")
        self.assertEqual(real.read_bytes(), raw)

    def test_invalid_utf8_and_nul_are_rejected_without_mutation(self) -> None:
        for raw, reason in ((b"\xff", "INVALID_UTF8"), (b"a\x00b", "BINARY_CONTENT")):
            with self.subTest(reason=reason):
                self._write(raw)
                with self.assertRaises(CapabilityFailure) as caught:
                    patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("a", "x")])
                self.assertEqual(caught.exception.reason_code, reason)
                self.assertEqual(self.path.read_bytes(), raw)

    def test_input_output_edit_count_and_aggregate_bounds(self) -> None:
        raw = self._write(b"a" * 33)
        edits = [self._edit("z", "x") for _ in range(21)]
        with self.assertRaises(CapabilityFailure) as too_many:
            patch_file(str(self.root), "target.txt", _sha(raw), edits)
        self.assertEqual(too_many.exception.reason_code, "INVALID_EDITS")

        huge = "x" * (MAX_EDIT_BYTES + 1)
        with self.assertRaises(CapabilityFailure) as edit_bytes:
            patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("a", huge)])
        self.assertEqual(edit_bytes.exception.reason_code, "EDIT_BYTES_LIMIT")

        with patch("agent_runtime.fs_patch.MAX_INPUT_FILE_BYTES", 4):
            self._write(b"alpha")
            with self.assertRaises(CapabilityFailure) as input_limit:
                patch_file(str(self.root), "target.txt", _sha(b"alpha"), [self._edit("a", "b")])
        self.assertEqual(input_limit.exception.reason_code, "INPUT_FILE_LIMIT")

        raw = self._write(b"a")
        with patch("agent_runtime.fs_patch.MAX_OUTPUT_FILE_BYTES", 4):
            with self.assertRaises(CapabilityFailure) as output_limit:
                patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("a", "12345")])
        self.assertEqual(output_limit.exception.reason_code, "OUTPUT_FILE_LIMIT")

    def test_mode_is_preserved_and_write_uses_fsync_then_atomic_replace_then_parent_fsync(self) -> None:
        raw = self._write(b"alpha\n", mode=0o751)
        real_fsync = os.fsync
        real_replace = os.replace
        events: list[tuple[str, int | str]] = []

        def observing_fsync(fd: int) -> None:
            mode = os.fstat(fd).st_mode
            events.append(("fsync", stat.S_IFMT(mode)))
            real_fsync(fd)

        def observing_replace(src: str, dst: str, **kwargs: object) -> None:
            events.append(("replace", dst))
            real_replace(src, dst, **kwargs)

        with patch("agent_runtime.fs_patch.os.fsync", side_effect=observing_fsync), patch(
            "agent_runtime.fs_patch.os.replace", side_effect=observing_replace
        ):
            patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("alpha", "beta")])

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o751)
        self.assertEqual(self.path.read_bytes(), b"beta\n")
        replace_index = next(i for i, event in enumerate(events) if event[0] == "replace")
        self.assertTrue(any(event == ("fsync", stat.S_IFREG) for event in events[:replace_index]))
        self.assertTrue(any(event == ("fsync", stat.S_IFDIR) for event in events[replace_index + 1 :]))

    def test_pre_replace_identity_or_hash_drift_is_state_changed_and_original_is_not_replaced(self) -> None:
        raw = self._write(b"alpha\n")
        real_revalidate = fs_patch_module._revalidate_target
        mutated = b"operator-change\n"

        def drift(parent_fd: int, final: str, identity: tuple[int, int], expected: str) -> None:
            self.path.write_bytes(mutated)
            real_revalidate(parent_fd, final, identity, expected)

        with patch("agent_runtime.fs_patch._revalidate_target", side_effect=drift):
            with self.assertRaises(CapabilityFailure) as caught:
                patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("alpha", "beta")])
        self.assertEqual(caught.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(caught.exception.reason_code, "LOCAL_STATE_CHANGED")
        self.assertEqual(self.path.read_bytes(), mutated)
        self.assertEqual(list(self.root.glob(".agent-runtime-patch-*.tmp")), [])

    def test_pre_replace_growth_beyond_input_bound_is_still_state_changed(self) -> None:
        raw = self._write(b"alpha\n")
        real_revalidate = fs_patch_module._revalidate_target

        def grow(parent_fd: int, final: str, identity: tuple[int, int], expected: str) -> None:
            self.path.write_bytes(b"x" * (MAX_INPUT_FILE_BYTES + 1))
            real_revalidate(parent_fd, final, identity, expected)

        with patch("agent_runtime.fs_patch._revalidate_target", side_effect=grow):
            with self.assertRaises(CapabilityFailure) as caught:
                patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("alpha", "beta")])
        self.assertEqual(caught.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(caught.exception.reason_code, "LOCAL_STATE_CHANGED")
        self.assertEqual(list(self.root.glob(".agent-runtime-patch-*.tmp")), [])

    def test_temp_file_is_same_directory_and_cleaned_on_write_failure(self) -> None:
        raw = self._write(b"alpha\n")
        original_write_all = fs_patch_module._write_all

        def fail_after_partial(fd: int, payload: bytes) -> None:
            os.write(fd, payload[:1])
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "INJECTED_WRITE_FAILURE",
                "injected write failure",
            )

        with patch("agent_runtime.fs_patch._write_all", side_effect=fail_after_partial):
            with self.assertRaises(CapabilityFailure):
                patch_file(str(self.root), "target.txt", _sha(raw), [self._edit("alpha", "beta")])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.root.glob(".agent-runtime-patch-*.tmp")), [])
        self.assertIsNotNone(original_write_all)


if __name__ == "__main__":
    unittest.main()
