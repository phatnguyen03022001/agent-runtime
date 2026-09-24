from __future__ import annotations

import hashlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from agent_runtime import server
from agent_runtime.capacity import HeavyExecutionAdmission
from agent_runtime.contracts import (
    FsPatchEdit,
    RepoStageItem,
    RuntimeToolErrorPayload,
)
from agent_runtime.errors import RuntimeCapacityError
from agent_runtime.fs_patch import patch_file
from agent_runtime.protection import ProtectedRuntimeDenied
from agent_runtime.repo_publish import RepoPublishFailure
from agent_runtime.repo_stage import stage_repository
from agent_runtime.session import TerminalSessionManager
from agent_runtime.tool_contract import ContractErrorCode

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
ERROR_FIELDS = {
    "code",
    "reason_code",
    "message",
    "retryable",
    "effect_state",
    "reconciliation_required",
    "safe_next_action",
}


def _error_payload(result: object) -> dict[str, object]:
    structured = getattr(result, "structured_content")
    if not isinstance(structured, dict):
        raise AssertionError(f"missing structured error content: {structured!r}")
    error = structured.get("error")
    if not isinstance(error, dict):
        raise AssertionError(f"missing structured error payload: {structured!r}")
    if set(error) != ERROR_FIELDS:
        raise AssertionError(f"unexpected error fields: {set(error)!r}")
    return error


def _assert_error(
    testcase: unittest.TestCase,
    result: object,
    *,
    code: str,
    reason_code: str,
    retryable: bool,
    effect_state: str,
    reconciliation_required: bool,
    safe_next_action: str,
) -> dict[str, object]:
    testcase.assertTrue(getattr(result, "is_error"))
    error = _error_payload(result)
    testcase.assertEqual(error["code"], code)
    testcase.assertEqual(error["reason_code"], reason_code)
    testcase.assertIs(error["retryable"], retryable)
    testcase.assertEqual(error["effect_state"], effect_state)
    testcase.assertIs(error["reconciliation_required"], reconciliation_required)
    testcase.assertEqual(error["safe_next_action"], safe_next_action)
    return error


class FailureEffectMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0144-", dir=str(WORKSPACE_ROOT))
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace),
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    async def test_invalid_schema_is_structured_tool_error_before_dispatch(self) -> None:
        with patch.object(server, "execute_terminal") as delegate:
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "terminal_exec",
                    {
                        "argv": ["/usr/bin/true"],
                        "cwd": str(self.workspace),
                        "start_identity": "0" * 32,
                        "timeout_seconds": 0,
                    },
                )
        delegate.assert_not_called()
        _assert_error(
            self,
            result,
            code="INVALID_ARGUMENT",
            reason_code="INVALID_REQUEST_SCHEMA",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="fix_request",
        )
        self.assertEqual(importlib.metadata.version("mcp"), "2.2.0")

    async def test_protected_authority_failure_is_absent_and_fix_request(self) -> None:
        with patch.object(
            server,
            "execute_terminal",
            side_effect=ProtectedRuntimeDenied("synthetic_protected"),
        ):
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "terminal_exec",
                    {
                        "argv": ["/usr/bin/true"],
                        "cwd": str(self.workspace),
                        "start_identity": "1" * 32,
                        "timeout_seconds": 1,
                    },
                )
        _assert_error(
            self,
            result,
            code="PERMISSION_DENIED",
            reason_code="PROTECTED_RUNTIME_DENIED",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="fix_request",
        )

    async def test_unknown_session_is_structured_not_found(self) -> None:
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "terminal_poll",
                {"session_id": "missing-session", "cursor": 0, "wait_ms": 0},
            )
        _assert_error(
            self,
            result,
            code="NOT_FOUND",
            reason_code="SESSION_UNKNOWN_OR_EXPIRED",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="fix_request",
        )

    async def test_unsupported_capability_is_not_internal_defect(self) -> None:
        async with Client(server.mcp) as client:
            result = await client.call_tool("screen_capture", {})
        _assert_error(
            self,
            result,
            code="UNAVAILABLE",
            reason_code="VISUAL_PERCEPTION_BLOCKED",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="unsupported",
        )

    async def test_unexpected_read_only_failure_is_absent_report_defect(self) -> None:
        sentinel = "TASK0144_READ_SECRET"
        with patch.object(server, "observe_capacity", side_effect=RuntimeError(sentinel)):
            async with Client(server.mcp) as client:
                result = await client.call_tool("capacity_observer", {})
        error = _assert_error(
            self,
            result,
            code="INTERNAL_ERROR",
            reason_code="UNEXPECTED_INTERNAL_ERROR",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="report_defect",
        )
        self.assertNotIn(sentinel, str(error))

    async def test_unexpected_effect_capable_failure_requires_reconciliation(self) -> None:
        sentinel = "TASK0144_EFFECT_SECRET"
        with patch.object(server, "execute_terminal", side_effect=RuntimeError(sentinel)):
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "terminal_exec",
                    {
                        "argv": ["/usr/bin/true"],
                        "cwd": str(self.workspace),
                        "start_identity": "2" * 32,
                        "timeout_seconds": 1,
                    },
                )
        error = _assert_error(
            self,
            result,
            code="INTERNAL_ERROR",
            reason_code="UNEXPECTED_INTERNAL_ERROR",
            retryable=False,
            effect_state="unknown",
            reconciliation_required=True,
            safe_next_action="reconcile",
        )
        self.assertNotIn(sentinel, str(error))


class FailureEffectBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="task-0144-", dir=str(WORKSPACE_ROOT))
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.cwd = self.workspace / "cwd"
        self.cwd.mkdir()
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace),
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_expected_sha_mismatch_is_pre_effect_conflict(self) -> None:
        target = self.cwd / "sample.txt"
        target.write_text("one\n", encoding="utf-8")
        with self.assertRaises(Exception) as raised:
            patch_file(
                str(self.cwd),
                "sample.txt",
                "0" * 64,
                [FsPatchEdit(old_text="one", new_text="two")],
            )
        result = server._runtime_error_from_exception("fs_patch", raised.exception)
        _assert_error(
            self,
            result,
            code="PRECONDITION_FAILED",
            reason_code="EXPECTED_SHA256_MISMATCH",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="fix_request",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "one\n")

    def test_atomic_patch_failure_before_effect_reports_absent(self) -> None:
        target = self.cwd / "sample.txt"
        target.write_text("one\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        with patch("agent_runtime.fs_patch.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(Exception) as raised:
                patch_file(
                    str(self.cwd),
                    "sample.txt",
                    digest,
                    [FsPatchEdit(old_text="one", new_text="two")],
                )
        result = server._runtime_error_from_exception("fs_patch", raised.exception)
        _assert_error(
            self,
            result,
            code="INTERNAL_ERROR",
            reason_code="ATOMIC_REPLACE_FAILED",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="report_defect",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "one\n")

    def test_patch_parent_fsync_failure_after_replace_reports_present(self) -> None:
        target = self.cwd / "sample.txt"
        target.write_text("one\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        fsync_calls = 0

        def fail_second_fsync(_fd: int) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError("synthetic parent fsync failure")

        with patch("agent_runtime.fs_patch.os.fsync", side_effect=fail_second_fsync):
            with self.assertRaises(Exception) as raised:
                patch_file(
                    str(self.cwd),
                    "sample.txt",
                    digest,
                    [FsPatchEdit(old_text="one", new_text="two")],
                )
        result = server._runtime_error_from_exception("fs_patch", raised.exception)
        _assert_error(
            self,
            result,
            code="INTERNAL_ERROR",
            reason_code="PARENT_FSYNC_FAILED",
            retryable=False,
            effect_state="present",
            reconciliation_required=True,
            safe_next_action="reconcile",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "two\n")

    def test_expected_git_head_mismatch_is_absent_before_index_mutation(self) -> None:
        repo = self.workspace / "repo"
        remote = self.workspace / "origin.git"
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(remote)], check=True)
        repo.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["/usr/bin/git", "config", "user.name", "Task 0144"], cwd=repo, check=True)
        subprocess.run(["/usr/bin/git", "config", "user.email", "task0144@example.invalid"], cwd=repo, check=True)
        target = repo / "tracked.txt"
        target.write_text("one\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["/usr/bin/git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
        subprocess.run(["/usr/bin/git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
        subprocess.run(["/usr/bin/git", "push", "-q", "-u", "origin", "main"], cwd=repo, check=True)
        target.write_text("two\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()

        with self.assertRaises(Exception) as raised:
            stage_repository(
                str(repo),
                "main",
                "0" * 40,
                [RepoStageItem(path="tracked.txt", operation="present", expected_sha256=digest)],
            )
        result = server._runtime_error_from_exception("repo_stage", raised.exception)
        _assert_error(
            self,
            result,
            code="PRECONDITION_FAILED",
            reason_code="HEAD_MISMATCH",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="fix_request",
        )
        staged = subprocess.run(
            ["/usr/bin/git", "diff", "--cached", "--quiet"],
            cwd=repo,
            check=False,
        )
        self.assertEqual(staged.returncode, 0)

    def test_capacity_rejection_occurs_before_process_dispatch(self) -> None:
        admission = HeavyExecutionAdmission(1)
        held = admission.acquire()
        self.addCleanup(held.release)
        manager = TerminalSessionManager(admission=admission, start_reaper=False)
        self.addCleanup(manager.shutdown)
        with patch("agent_runtime.session.subprocess.Popen") as popen:
            with self.assertRaises(RuntimeCapacityError) as raised:
                manager.start([sys.executable, "-c", "pass"], str(self.cwd))
            popen.assert_not_called()
        result = server._runtime_error_from_exception("terminal_start", raised.exception)
        _assert_error(
            self,
            result,
            code="LIMIT_EXCEEDED",
            reason_code="CAPACITY_EXHAUSTED",
            retryable=True,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="wait",
        )

    def test_pre_effect_process_start_failure_is_absent_and_keyed(self) -> None:
        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1),
            start_reaper=False,
        )
        self.addCleanup(manager.shutdown)
        identity = "8" * 32
        with patch("agent_runtime.session.pty.openpty", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError) as raised:
                manager.start([sys.executable, "-c", "pass"], str(self.cwd), identity)
        result = server._runtime_error_from_exception("terminal_start", raised.exception)
        _assert_error(
            self,
            result,
            code="PRECONDITION_FAILED",
            reason_code="PROCESS_START_FAILED_PRE_EFFECT",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="fix_request",
        )
        retained = manager.poll(start_identity=identity)
        self.assertEqual(retained["lifecycle"], "START_FAILED_PRE_EFFECT")

    def test_post_effect_start_failure_is_unknown_and_reconcile(self) -> None:
        manager = TerminalSessionManager(
            admission=HeavyExecutionAdmission(1),
            start_reaper=False,
        )
        self.addCleanup(manager.shutdown)
        identity = "d" * 32
        with patch(
            "agent_runtime.session.threading.Thread.start",
            side_effect=RuntimeError("synthetic post effect"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                manager.start(
                    [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                    str(self.cwd),
                    identity,
                )
        result = server._runtime_error_from_exception("terminal_start", raised.exception)
        _assert_error(
            self,
            result,
            code="INTERNAL_ERROR",
            reason_code="PROCESS_START_FAILED_POST_EFFECT",
            retryable=False,
            effect_state="unknown",
            reconciliation_required=True,
            safe_next_action="reconcile",
        )
        retained = manager.poll(start_identity=identity)
        self.assertEqual(retained["lifecycle"], "START_FAILED_POST_EFFECT")

    def test_repo_push_failure_after_unchanged_remote_allows_retry(self) -> None:
        failure = RepoPublishFailure(
            "PUSH_FAILED",
            "push failed and fresh observation proved the remote head remained unchanged",
            retryable=True,
        )
        result = server._runtime_error_from_exception("repo_publish", failure)
        _assert_error(
            self,
            result,
            code="UNAVAILABLE",
            reason_code="PUSH_FAILED",
            retryable=True,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="retry",
        )

    def test_repo_publication_ambiguity_never_advertises_blind_retry(self) -> None:
        failure = RepoPublishFailure(
            "PUBLICATION_AMBIGUOUS",
            "post-push remote state could not be established",
            retryable=True,
        )
        result = server._runtime_error_from_exception("repo_publish", failure)
        _assert_error(
            self,
            result,
            code="UNAVAILABLE",
            reason_code="PUBLICATION_AMBIGUOUS",
            retryable=False,
            effect_state="unknown",
            reconciliation_required=True,
            safe_next_action="reconcile",
        )

    def test_message_is_bounded_sanitized_and_not_needed_for_action(self) -> None:
        result = server._runtime_error_result(
            tool_name="capacity_observer",
            code=ContractErrorCode.INTERNAL_ERROR,
            reason_code="SYNTHETIC_INTERNAL",
            message="  diagnostic\n" + ("x" * 400),
            retryable=False,
        )
        error = _assert_error(
            self,
            result,
            code="INTERNAL_ERROR",
            reason_code="SYNTHETIC_INTERNAL",
            retryable=False,
            effect_state="absent",
            reconciliation_required=False,
            safe_next_action="report_defect",
        )
        message = str(error["message"])
        self.assertLessEqual(len(message), 256)
        self.assertNotIn("\n", message)

    def test_serialization_is_exact_and_known_present_is_distinct_from_success(self) -> None:
        payload = RuntimeToolErrorPayload(
            code=ContractErrorCode.INTERNAL_ERROR,
            reason_code="KNOWN_EFFECT_PRESENT",
            message="effect exists; higher-level verification remains separate",
            retryable=False,
            effect_state="present",
            reconciliation_required=False,
            safe_next_action="report_defect",
        )
        serialized = payload.model_dump(mode="json")
        self.assertEqual(set(serialized), ERROR_FIELDS)
        self.assertEqual(serialized["effect_state"], "present")
        self.assertEqual(serialized["safe_next_action"], "report_defect")
        with self.assertRaises(ValueError):
            RuntimeToolErrorPayload(
                code=ContractErrorCode.UNAVAILABLE,
                reason_code="LOST_OUTCOME",
                message="lost outcome",
                retryable=True,
                effect_state="unknown",
                reconciliation_required=True,
                safe_next_action="reconcile",
            )

    def test_failure_inventory_covers_exact_public_surface(self) -> None:
        self.assertEqual(set(server._PUBLIC_TOOL_EFFECT_RISK), set(server.PUBLIC_TOOL_NAMES))
        self.assertEqual(len(server._PUBLIC_TOOL_EFFECT_RISK), 19)


if __name__ == "__main__":
    unittest.main()
