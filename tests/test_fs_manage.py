from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from agent_runtime import fs_manage, server
from agent_runtime.contracts import FsManageResult
from agent_runtime.fs_manage import manage_filesystem
from agent_runtime.tool_contract import ContractErrorCode

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FsManageBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0148-manage-", dir=str(WORKSPACE_ROOT))
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.cwd = self.workspace / "cwd"
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_mkdir_single_parents_existing_and_parent_bound(self) -> None:
        result = manage_filesystem(str(self.cwd), "mkdir", path="single")
        self.assertIsInstance(result, FsManageResult)
        self.assertEqual(result.operation, "mkdir")
        self.assertEqual(result.effect_state, "present")
        self.assertEqual([item.path for item in result.before], ["single"])
        self.assertEqual([item.kind for item in result.before], ["absent"])
        self.assertEqual([item.kind for item in result.after], ["directory"])
        self.assertTrue((self.cwd / "single").is_dir())

        with self.assertRaises(Exception) as raised:
            manage_filesystem(str(self.cwd), "mkdir", path="single")
        self.assertEqual(raised.exception.code, ContractErrorCode.CONFLICT)

        with self.assertRaises(Exception) as raised:
            manage_filesystem(str(self.cwd), "mkdir", path="missing/child")
        self.assertEqual(raised.exception.reason_code, "PARENT_NOT_FOUND")
        self.assertFalse((self.cwd / "missing").exists())

        nested = "/".join(f"d{index}" for index in range(16))
        created = manage_filesystem(str(self.cwd), "mkdir", path=nested, parents=True)
        self.assertEqual(len(created.after), 16)
        self.assertTrue(self.cwd.joinpath(*nested.split("/")).is_dir())

        too_deep = "/".join(f"x{index}" for index in range(17))
        with self.assertRaises(Exception) as raised:
            manage_filesystem(str(self.cwd), "mkdir", path=too_deep, parents=True)
        self.assertEqual(raised.exception.reason_code, "MKDIR_PARENT_LIMIT")
        self.assertFalse((self.cwd / "x0").exists())

    def test_move_regular_file_is_atomic_no_overwrite_and_verified(self) -> None:
        source = self.cwd / "source.txt"
        source.write_text("payload\n", encoding="utf-8")
        digest = _sha(source)
        before_identity = (source.stat().st_dev, source.stat().st_ino)

        result = manage_filesystem(
            str(self.cwd),
            "move",
            source_path="source.txt",
            destination_path="destination.txt",
            expected_kind="file",
            expected_source_sha256=digest,
        )

        destination = self.cwd / "destination.txt"
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_text(encoding="utf-8"), "payload\n")
        self.assertEqual((destination.stat().st_dev, destination.stat().st_ino), before_identity)
        self.assertEqual(result.before[0].sha256, digest)
        self.assertEqual(result.before[1].kind, "absent")
        self.assertEqual(result.after[0].kind, "absent")
        self.assertEqual(result.after[1].sha256, digest)
        self.assertEqual(result.effect_state, "present")

    def test_move_directory_preserves_identity(self) -> None:
        source = self.cwd / "source-dir"
        source.mkdir()
        (source / "child.txt").write_text("child", encoding="utf-8")
        identity = (source.stat().st_dev, source.stat().st_ino)

        result = manage_filesystem(
            str(self.cwd),
            "move",
            source_path="source-dir",
            destination_path="destination-dir",
            expected_kind="directory",
        )

        destination = self.cwd / "destination-dir"
        self.assertFalse(source.exists())
        self.assertEqual((destination.stat().st_dev, destination.stat().st_ino), identity)
        self.assertEqual((destination / "child.txt").read_text(encoding="utf-8"), "child")
        self.assertEqual(result.after[1].kind, "directory")

    def test_move_source_replacement_is_state_changed_before_effect(self) -> None:
        source = self.cwd / "source.txt"
        source.write_text("original", encoding="utf-8")
        digest = _sha(source)
        original = fs_manage._revalidate_file
        replaced = False

        def replace_then_revalidate(parent_fd, name, expected_identity, expected_sha256):
            nonlocal replaced
            if not replaced:
                replaced = True
                source.unlink()
                source.write_text("replacement", encoding="utf-8")
            return original(parent_fd, name, expected_identity, expected_sha256)

        with patch("agent_runtime.fs_manage._revalidate_file", side_effect=replace_then_revalidate):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(
                    str(self.cwd),
                    "move",
                    source_path="source.txt",
                    destination_path="destination.txt",
                    expected_kind="file",
                    expected_source_sha256=digest,
                )

        self.assertTrue(replaced)
        self.assertEqual(raised.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(raised.exception.effect_state, None)
        self.assertEqual(source.read_text(encoding="utf-8"), "replacement")
        self.assertFalse((self.cwd / "destination.txt").exists())

    def test_move_destination_race_is_conflict_without_overwrite(self) -> None:
        source = self.cwd / "source.txt"
        destination = self.cwd / "destination.txt"
        source.write_text("source", encoding="utf-8")
        digest = _sha(source)
        original = fs_manage._rename_no_replace

        def race(source_parent_fd, source_name, destination_parent_fd, destination_name):
            destination.write_text("racer", encoding="utf-8")
            return original(source_parent_fd, source_name, destination_parent_fd, destination_name)

        with patch("agent_runtime.fs_manage._rename_no_replace", side_effect=race):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(
                    str(self.cwd),
                    "move",
                    source_path="source.txt",
                    destination_path="destination.txt",
                    expected_kind="file",
                    expected_source_sha256=digest,
                )

        self.assertEqual(raised.exception.code, ContractErrorCode.CONFLICT)
        self.assertEqual(raised.exception.reason_code, "DESTINATION_ALREADY_EXISTS")
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertEqual(destination.read_text(encoding="utf-8"), "racer")

    def test_move_postcondition_failure_requires_reconciliation(self) -> None:
        source = self.cwd / "source-post.txt"
        source.write_text("source", encoding="utf-8")
        digest = _sha(source)
        original = fs_manage._open_file

        def fail_destination(parent_fd, name, path, expected_sha256, *, mismatch_reason):
            if path == "destination-post.txt":
                raise fs_manage._failure(
                    ContractErrorCode.UNAVAILABLE,
                    "SYNTHETIC_POSTCONDITION_FAILURE",
                    "synthetic postcondition failure",
                )
            return original(
                parent_fd,
                name,
                path,
                expected_sha256,
                mismatch_reason=mismatch_reason,
            )

        with patch("agent_runtime.fs_manage._open_file", side_effect=fail_destination):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(
                    str(self.cwd),
                    "move",
                    source_path="source-post.txt",
                    destination_path="destination-post.txt",
                    expected_kind="file",
                    expected_source_sha256=digest,
                )

        self.assertEqual(raised.exception.effect_state.value, "present")
        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(raised.exception.safe_next_action.value, "reconcile")
        self.assertFalse(source.exists())
        self.assertTrue((self.cwd / "destination-post.txt").exists())

    def test_unknown_move_failure_is_unknown_and_requires_reconciliation(self) -> None:
        source = self.cwd / "source-unknown.txt"
        source.write_text("source", encoding="utf-8")
        digest = _sha(source)

        with patch("agent_runtime.fs_manage._rename_no_replace", side_effect=OSError(errno.EIO, "synthetic")):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(
                    str(self.cwd),
                    "move",
                    source_path="source-unknown.txt",
                    destination_path="destination-unknown.txt",
                    expected_kind="file",
                    expected_source_sha256=digest,
                )

        self.assertEqual(raised.exception.effect_state.value, "unknown")
        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(raised.exception.safe_next_action.value, "reconcile")

    def test_move_exdev_is_pre_effect_and_has_no_copy_delete_fallback(self) -> None:
        source = self.cwd / "source.txt"
        source.write_text("source", encoding="utf-8")
        digest = _sha(source)

        with patch("agent_runtime.fs_manage._rename_no_replace", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(
                    str(self.cwd),
                    "move",
                    source_path="source.txt",
                    destination_path="destination.txt",
                    expected_kind="file",
                    expected_source_sha256=digest,
                )

        self.assertEqual(raised.exception.reason_code, "CROSS_DEVICE_MOVE")
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertFalse((self.cwd / "destination.txt").exists())

    def test_delete_file_and_empty_directory_only(self) -> None:
        target = self.cwd / "target.txt"
        target.write_text("delete-me", encoding="utf-8")
        digest = _sha(target)
        deleted = manage_filesystem(
            str(self.cwd),
            "delete",
            path="target.txt",
            expected_kind="file",
            expected_sha256=digest,
        )
        self.assertFalse(target.exists())
        self.assertEqual(deleted.before[0].sha256, digest)
        self.assertEqual(deleted.after[0].kind, "absent")

        empty = self.cwd / "empty"
        empty.mkdir()
        removed = manage_filesystem(
            str(self.cwd),
            "delete",
            path="empty",
            expected_kind="directory",
        )
        self.assertFalse(empty.exists())
        self.assertEqual(removed.after[0].kind, "absent")

        nonempty = self.cwd / "nonempty"
        nonempty.mkdir()
        (nonempty / "child.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(Exception) as raised:
            manage_filesystem(
                str(self.cwd),
                "delete",
                path="nonempty",
                expected_kind="directory",
            )
        self.assertEqual(raised.exception.reason_code, "DIRECTORY_NOT_EMPTY")
        self.assertTrue((nonempty / "child.txt").exists())

    def test_delete_revalidates_file_before_unlink(self) -> None:
        target = self.cwd / "target.txt"
        target.write_text("original", encoding="utf-8")
        digest = _sha(target)
        original = fs_manage._revalidate_file
        replaced = False

        def replace_then_revalidate(parent_fd, name, expected_identity, expected_sha256):
            nonlocal replaced
            if not replaced:
                replaced = True
                target.unlink()
                target.write_text("replacement", encoding="utf-8")
            return original(parent_fd, name, expected_identity, expected_sha256)

        with patch("agent_runtime.fs_manage._revalidate_file", side_effect=replace_then_revalidate):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(
                    str(self.cwd),
                    "delete",
                    path="target.txt",
                    expected_kind="file",
                    expected_sha256=digest,
                )

        self.assertEqual(raised.exception.code, ContractErrorCode.STATE_CHANGED)
        self.assertEqual(target.read_text(encoding="utf-8"), "replacement")

    def test_chmod_preserves_identity_and_content_and_rejects_special_bits(self) -> None:
        target = self.cwd / "mode.txt"
        target.write_text("stable", encoding="utf-8")
        target.chmod(0o600)
        digest = _sha(target)
        identity = (target.stat().st_dev, target.stat().st_ino)

        result = manage_filesystem(
            str(self.cwd),
            "chmod",
            path="mode.txt",
            expected_sha256=digest,
            mode=0o640,
        )

        self.assertEqual((target.stat().st_dev, target.stat().st_ino), identity)
        self.assertEqual(_sha(target), digest)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(result.before[0].mode, 0o600)
        self.assertEqual(result.after[0].mode, 0o640)
        self.assertEqual(result.effect_state, "present")

        with self.assertRaises(Exception) as raised:
            manage_filesystem(
                str(self.cwd),
                "chmod",
                path="mode.txt",
                expected_sha256=digest,
                mode=0o1640,
            )
        self.assertEqual(raised.exception.reason_code, "INVALID_OPERATION_ARGUMENTS")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_invalid_paths_fail_before_filesystem_effects(self) -> None:
        for invalid in ("/absolute", "../escape", "a/../escape", "a//b", "a/./b", "nul\x00name"):
            with self.subTest(path=repr(invalid)):
                with self.assertRaises(Exception) as raised:
                    manage_filesystem(str(self.cwd), "mkdir", path=invalid)
                self.assertEqual(raised.exception.code, ContractErrorCode.INVALID_ARGUMENT)

        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_symlink_outside_workspace_and_protected_runtime_fail_closed(self) -> None:
        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (self.cwd / "link").symlink_to(outside / "secret.txt")

        with self.assertRaises(Exception) as raised:
            manage_filesystem(
                str(self.cwd),
                "delete",
                path="link",
                expected_kind="file",
                expected_sha256=_sha(outside / "secret.txt"),
            )
        self.assertEqual(raised.exception.reason_code, "SYMLINK_DISALLOWED")
        self.assertTrue((outside / "secret.txt").exists())

        foreign = self.workspace.parent / f"{self.workspace.name}-foreign"
        foreign.mkdir()
        self.addCleanup(lambda: foreign.rmdir() if foreign.exists() else None)
        with self.assertRaises(Exception) as raised:
            manage_filesystem(str(foreign), "mkdir", path="denied")
        self.assertEqual(raised.exception.code, ContractErrorCode.OUTSIDE_WORKSPACE)
        self.assertFalse((foreign / "denied").exists())

        protected = self.cwd / "protected-runtime"
        protected.mkdir()
        with patch("agent_runtime.protection._default_runtime_root", return_value=protected):
            with self.assertRaises(Exception) as raised:
                manage_filesystem(str(self.cwd), "mkdir", path="protected-runtime/child")
        self.assertEqual(type(raised.exception).__name__, "ProtectedRuntimeDenied")
        self.assertFalse((protected / "child").exists())


class FsManageMCPContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0148-mcp-", dir=str(WORKSPACE_ROOT))
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.cwd = self.workspace / "cwd"
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace)})
        self._env.start()
        self.addCleanup(self._env.stop)

    async def test_schema_annotations_and_structured_conflict_effect(self) -> None:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            tool = tools["fs_manage"]
            annotations = tool.annotations.model_dump(by_alias=True)
            self.assertEqual(
                (
                    annotations["readOnlyHint"],
                    annotations["destructiveHint"],
                    annotations["idempotentHint"],
                    annotations["openWorldHint"],
                ),
                (False, True, False, False),
            )
            self.assertFalse(tool.input_schema["additionalProperties"])
            properties = tool.input_schema["properties"]
            self.assertEqual(properties["operation"]["enum"], ["mkdir", "move", "delete", "chmod"])
            self.assertEqual(properties["mode"]["anyOf"][0]["maximum"], 0o777)

            first = await client.call_tool(
                "fs_manage",
                {"cwd": str(self.cwd), "operation": "mkdir", "path": "created"},
            )
            conflict = await client.call_tool(
                "fs_manage",
                {"cwd": str(self.cwd), "operation": "mkdir", "path": "created"},
            )

        self.assertFalse(first.is_error)
        self.assertEqual(first.structured_content["operation"], "mkdir")
        self.assertEqual(first.structured_content["effect_state"], "present")
        self.assertTrue(conflict.is_error)
        error = conflict.structured_content["error"]
        self.assertEqual(error["code"], "CONFLICT")
        self.assertEqual(error["reason_code"], "TARGET_ALREADY_EXISTS")
        self.assertEqual(error["effect_state"], "absent")
        self.assertFalse(error["reconciliation_required"])
        self.assertEqual(error["safe_next_action"], "fix_request")


if __name__ == "__main__":
    unittest.main()
