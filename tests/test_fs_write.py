from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_runtime.fs_write as fs_write_module
from agent_runtime.contracts import CapabilityFailure, FsWriteReceiptResult, ReceiptV1Result
from agent_runtime.fs_write import MAX_CONTENT_BYTES, write_file
from agent_runtime.tool_contract import ContractErrorCode


class FsWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self._env = patch.dict(
            os.environ,
            {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.root)},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _failure(self, callable_) -> CapabilityFailure:
        with self.assertRaises(CapabilityFailure) as caught:
            callable_()
        return caught.exception

    def test_create_writes_exact_strict_utf8_without_newline_normalization(self) -> None:
        content = "héllo\r\nworld\r"
        result = write_file(str(self.root), "exact.txt", "create", content)
        self.assertEqual(result.status, "created")
        self.assertEqual((self.root / "exact.txt").read_bytes(), content.encode("utf-8"))
        self.assertEqual(result.sha256_after, hashlib.sha256(content.encode()).hexdigest())

    def test_create_rejects_invalid_surrogate(self) -> None:
        exc = self._failure(
            lambda: write_file(str(self.root), "bad.txt", "create", "\ud800")
        )
        self.assertIs(exc.code, ContractErrorCode.INVALID_ARGUMENT)
        self.assertEqual(exc.reason_code, "INVALID_UTF8_CONTENT")

    def test_create_rejects_nul(self) -> None:
        exc = self._failure(
            lambda: write_file(str(self.root), "bad.txt", "create", "a\x00b")
        )
        self.assertEqual(exc.reason_code, "NUL_CONTENT_DISALLOWED")

    def test_create_enforces_exact_utf8_byte_limit(self) -> None:
        content = "é" * (MAX_CONTENT_BYTES // 2 + 1)
        exc = self._failure(
            lambda: write_file(str(self.root), "large.txt", "create", content)
        )
        self.assertIs(exc.code, ContractErrorCode.LIMIT_EXCEEDED)
        self.assertEqual(exc.reason_code, "OUTPUT_FILE_LIMIT")

    def test_create_requires_existing_parent(self) -> None:
        exc = self._failure(
            lambda: write_file(str(self.root), "missing/child.txt", "create", "x")
        )
        self.assertEqual(exc.reason_code, "TARGET_NOT_FOUND")
        self.assertFalse((self.root / "missing").exists())

    def test_create_rejects_parent_symlink_traversal(self) -> None:
        real = self.root / "real"
        real.mkdir()
        (self.root / "linked").symlink_to(real, target_is_directory=True)
        exc = self._failure(
            lambda: write_file(str(self.root), "linked/child.txt", "create", "x")
        )
        self.assertEqual(exc.reason_code, "SYMLINK_DISALLOWED")
        self.assertFalse((real / "child.txt").exists())

    def test_create_existing_file_directory_and_symlink_never_mutate(self) -> None:
        file_target = self.root / "file.txt"
        file_target.write_text("old")
        dir_target = self.root / "dir"
        dir_target.mkdir()
        link_target = self.root / "link"
        link_target.symlink_to(file_target)
        for name in ("file.txt", "dir", "link"):
            with self.subTest(name=name):
                exc = self._failure(
                    lambda name=name: write_file(str(self.root), name, "create", "new")
                )
                self.assertEqual(exc.reason_code, "TARGET_ALREADY_EXISTS")
        self.assertEqual(file_target.read_text(), "old")
        self.assertTrue(dir_target.is_dir())
        self.assertTrue(link_target.is_symlink())

    def test_create_target_appearance_race_is_no_overwrite(self) -> None:
        target = self.root / "race.txt"

        def race(*args, **kwargs):
            target.write_text("racer")
            raise FileExistsError()

        with patch("agent_runtime.fs_write.os.link", side_effect=race):
            exc = self._failure(
                lambda: write_file(str(self.root), "race.txt", "create", "ours")
            )
        self.assertEqual(exc.reason_code, "TARGET_ALREADY_EXISTS")
        self.assertEqual(target.read_text(), "racer")

    def test_create_uses_same_directory_atomic_link_and_fsyncs_file_and_parent(self) -> None:
        real_link = os.link
        real_fsync = os.fsync
        with patch("agent_runtime.fs_write.os.link", wraps=real_link) as link_mock, patch(
            "agent_runtime.fs_write.os.fsync", wraps=real_fsync
        ) as fsync_mock:
            write_file(str(self.root), "atomic.txt", "create", "payload")
        self.assertEqual(link_mock.call_count, 1)
        kwargs = link_mock.call_args.kwargs
        self.assertEqual(kwargs["src_dir_fd"], kwargs["dst_dir_fd"])
        self.assertFalse(kwargs["follow_symlinks"])
        self.assertGreaterEqual(fsync_mock.call_count, 2)

    def test_create_mode_is_0666_subject_to_umask(self) -> None:
        previous = os.umask(0o027)
        try:
            result = write_file(str(self.root), "mode.txt", "create", "x")
        finally:
            os.umask(previous)
        self.assertEqual(result.mode_after, 0o640)
        self.assertEqual(stat.S_IMODE((self.root / "mode.txt").stat().st_mode), 0o640)

    def test_create_success_removes_temporary_name(self) -> None:
        write_file(str(self.root), "clean.txt", "create", "x")
        self.assertEqual([p.name for p in self.root.iterdir()], ["clean.txt"])

    def test_create_cleanup_failure_returns_ambiguous_without_unlinking_target(self) -> None:
        real_unlink = os.unlink

        def fail_temp(path, *args, **kwargs):
            if str(path).startswith(".agent-runtime-write-"):
                raise OSError("forced temp cleanup failure")
            return real_unlink(path, *args, **kwargs)

        with patch("agent_runtime.fs_write.os.unlink", side_effect=fail_temp):
            exc = self._failure(
                lambda: write_file(str(self.root), "published.txt", "create", "x")
            )
        self.assertIs(exc.code, ContractErrorCode.INTERNAL_ERROR)
        self.assertEqual(exc.reason_code, "CREATE_CLEANUP_AMBIGUOUS")
        self.assertEqual((self.root / "published.txt").read_text(), "x")
        for item in self.root.iterdir():
            if item.name.startswith(".agent-runtime-write-"):
                item.unlink()

    def test_create_replay_cannot_mutate(self) -> None:
        write_file(str(self.root), "replay.txt", "create", "first")
        exc = self._failure(
            lambda: write_file(str(self.root), "replay.txt", "create", "second")
        )
        self.assertEqual(exc.reason_code, "TARGET_ALREADY_EXISTS")
        self.assertEqual((self.root / "replay.txt").read_text(), "first")

    def test_create_forbids_expected_sha(self) -> None:
        exc = self._failure(
            lambda: write_file(str(self.root), "x.txt", "create", "x", "0" * 64)
        )
        self.assertEqual(exc.reason_code, "EXPECTED_SHA_FORBIDDEN")

    def test_replace_requires_expected_sha(self) -> None:
        (self.root / "x.txt").write_text("old")
        exc = self._failure(
            lambda: write_file(str(self.root), "x.txt", "replace", "new")
        )
        self.assertEqual(exc.reason_code, "EXPECTED_SHA_REQUIRED")

    def test_replace_expected_sha_mismatch_does_not_mutate(self) -> None:
        target = self.root / "x.txt"
        target.write_text("old")
        exc = self._failure(
            lambda: write_file(str(self.root), "x.txt", "replace", "new", "0" * 64)
        )
        self.assertEqual(exc.reason_code, "EXPECTED_SHA_MISMATCH")
        self.assertEqual(target.read_text(), "old")

    def test_replace_rejects_non_utf8_nul_and_oversized_targets(self) -> None:
        cases = (
            (b"\xff", "INVALID_UTF8_TARGET"),
            (b"a\x00b", "NUL_TARGET_DISALLOWED"),
            (b"a" * (MAX_CONTENT_BYTES + 1), "INPUT_FILE_LIMIT"),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                target = self.root / "target.txt"
                target.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                exc = self._failure(
                    lambda digest=digest: write_file(
                        str(self.root), "target.txt", "replace", "new", digest
                    )
                )
                self.assertEqual(exc.reason_code, reason)
                self.assertEqual(target.read_bytes(), raw)

    def test_replace_rejects_symlink_and_non_regular_target(self) -> None:
        real = self.root / "real.txt"
        real.write_text("old")
        (self.root / "link.txt").symlink_to(real)
        (self.root / "dir").mkdir()
        for name, reason in (("link.txt", "SYMLINK_DISALLOWED"), ("dir", "NOT_REGULAR_FILE")):
            with self.subTest(name=name):
                exc = self._failure(
                    lambda name=name: write_file(
                        str(self.root), name, "replace", "new", hashlib.sha256(b"old").hexdigest()
                    )
                )
                self.assertEqual(exc.reason_code, reason)

    def test_replace_preserves_exact_mode_bits(self) -> None:
        target = self.root / "mode.txt"
        target.write_text("old")
        target.chmod(0o640)
        digest = hashlib.sha256(b"old").hexdigest()
        result = write_file(str(self.root), "mode.txt", "replace", "new", digest)
        self.assertEqual(result.status, "replaced")
        self.assertEqual(result.mode_before, 0o640)
        self.assertEqual(result.mode_after, 0o640)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_replace_same_bytes_revalidates_and_does_not_replace(self) -> None:
        target = self.root / "same.txt"
        target.write_text("same")
        digest = hashlib.sha256(b"same").hexdigest()
        with patch("agent_runtime.fs_write.os.replace") as replace_mock:
            result = write_file(str(self.root), "same.txt", "replace", "same", digest)
        self.assertEqual(result.status, "unchanged")
        replace_mock.assert_not_called()
        self.assertEqual(target.read_text(), "same")

    def test_replace_identity_drift_returns_state_changed_without_tool_replacement(self) -> None:
        target = self.root / "drift.txt"
        target.write_text("old")
        digest = hashlib.sha256(b"old").hexdigest()
        replacement = self.root / "other.txt"
        replacement.write_text("external")
        real_revalidate = fs_write_module._revalidate_target

        def drift(parent_fd, final, identity, sha):
            os.replace(replacement, target)
            return real_revalidate(parent_fd, final, identity, sha)

        with patch("agent_runtime.fs_write._revalidate_target", side_effect=drift):
            exc = self._failure(
                lambda: write_file(str(self.root), "drift.txt", "replace", "ours", digest)
            )
        self.assertIs(exc.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(exc.reason_code, "LOCAL_STATE_CHANGED")
        self.assertEqual(target.read_text(), "external")

    def test_replace_content_drift_returns_state_changed_without_tool_replacement(self) -> None:
        target = self.root / "drift.txt"
        target.write_text("old")
        digest = hashlib.sha256(b"old").hexdigest()
        real_revalidate = fs_write_module._revalidate_target

        def drift(parent_fd, final, identity, sha):
            target.write_text("external")
            return real_revalidate(parent_fd, final, identity, sha)

        with patch("agent_runtime.fs_write._revalidate_target", side_effect=drift):
            exc = self._failure(
                lambda: write_file(str(self.root), "drift.txt", "replace", "ours", digest)
            )
        self.assertEqual(exc.reason_code, "LOCAL_STATE_CHANGED")
        self.assertEqual(target.read_text(), "external")

    def test_replace_uses_atomic_replace_and_parent_fsync(self) -> None:
        target = self.root / "atomic.txt"
        target.write_text("old")
        digest = hashlib.sha256(b"old").hexdigest()
        real_replace = os.replace
        real_fsync = os.fsync
        with patch("agent_runtime.fs_write.os.replace", wraps=real_replace) as replace_mock, patch(
            "agent_runtime.fs_write.os.fsync", wraps=real_fsync
        ) as fsync_mock:
            result = write_file(str(self.root), "atomic.txt", "replace", "new", digest)
        self.assertEqual(result.status, "replaced")
        self.assertEqual(replace_mock.call_count, 1)
        kwargs = replace_mock.call_args.kwargs
        self.assertEqual(kwargs["src_dir_fd"], kwargs["dst_dir_fd"])
        self.assertGreaterEqual(fsync_mock.call_count, 2)

    def test_replace_replay_with_stale_sha_cannot_mutate(self) -> None:
        target = self.root / "replay.txt"
        target.write_text("old")
        old_sha = hashlib.sha256(b"old").hexdigest()
        write_file(str(self.root), "replay.txt", "replace", "new", old_sha)
        exc = self._failure(
            lambda: write_file(str(self.root), "replay.txt", "replace", "again", old_sha)
        )
        self.assertEqual(exc.reason_code, "EXPECTED_SHA_MISMATCH")
        self.assertEqual(target.read_text(), "new")

    def test_receipt_is_deterministic_and_binds_content_mode_and_operation(self) -> None:
        common = dict(
            checked_cwd=str(self.root),
            normalized_path="x.txt",
            mode_bits=0o640,
            final_bytes=b"abc",
        )
        create_a = fs_write_module._receipt(
            **common, operation="create", expected_sha256=None
        )
        create_b = fs_write_module._receipt(
            **common, operation="create", expected_sha256=None
        )
        content_changed = fs_write_module._receipt(
            **{**common, "final_bytes": b"abd"}, operation="create", expected_sha256=None
        )
        mode_changed = fs_write_module._receipt(
            **{**common, "mode_bits": 0o600}, operation="create", expected_sha256=None
        )
        replace = fs_write_module._receipt(
            **common,
            operation="replace",
            expected_sha256=hashlib.sha256(b"old").hexdigest(),
        )
        self.assertEqual(create_a.kind, "fs-write")
        self.assertEqual(create_a.digest, create_b.digest)
        self.assertNotEqual(create_a.digest, content_changed.digest)
        self.assertNotEqual(create_a.digest, mode_changed.digest)
        self.assertNotEqual(create_a.digest, replace.digest)

    def test_fs_write_receipt_model_does_not_broaden_repo_diff_receipt_kind(self) -> None:
        self.assertEqual(
            FsWriteReceiptResult.model_json_schema()["properties"]["kind"]["const"],
            "fs-write",
        )
        self.assertEqual(
            ReceiptV1Result.model_json_schema()["properties"]["kind"]["const"],
            "repo-diff",
        )


if __name__ == "__main__":
    unittest.main()
