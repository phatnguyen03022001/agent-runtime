from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from agent_runtime import server
from agent_runtime.repo_commit import REPO_COMMIT_CONTRACT
from agent_runtime.repo_stage import REPO_STAGE_CONTRACT

GIT = "/usr/bin/git"
EXPECTED_TOOLS = (
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
    "repo_diff",
    "repo_stage",
    "repo_commit",
    "repo_fast_forward",
    "repo_publish",
    "screen_capture",
    "runtime_capabilities",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )


def _annotations(tool: object) -> tuple[bool, bool, bool, bool]:
    values = tool.annotations.model_dump(by_alias=True)
    return (
        values["readOnlyHint"],
        values["destructiveHint"],
        values["idempotentHint"],
        values["openWorldHint"],
    )


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class RepoStageCommitMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = Path(self._temp.name).resolve()
        self.repo = self.workspace / "repo"
        self.origin = self.workspace / "origin.git"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Fixture")
        _git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "base")
        _git(self.repo, "branch", "-M", "main")
        _git(self.workspace, "init", "-q", "--bare", str(self.origin))
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        self._env = patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(self.workspace),
                "AGENT_RUNTIME_MAX_PARALLELISM": "2",
                "AGENT_RUNTIME_GIT_NAME": "Agent Runtime",
                "AGENT_RUNTIME_GIT_EMAIL": "runtime@example.invalid",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    async def _tools(self) -> dict[str, object]:
        return {tool.name: tool for tool in await server.mcp.list_tools()}

    def _head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()

    async def test_exact_18_tool_surface_and_contract_annotations(self) -> None:
        tools = await self._tools()
        self.assertEqual(tuple(tools), EXPECTED_TOOLS)
        self.assertEqual(server.PUBLIC_TOOL_NAMES, EXPECTED_TOOLS)
        self.assertEqual(
            _annotations(tools["repo_stage"]),
            (
                REPO_STAGE_CONTRACT.annotations.read_only,
                REPO_STAGE_CONTRACT.annotations.destructive,
                REPO_STAGE_CONTRACT.annotations.idempotent,
                REPO_STAGE_CONTRACT.annotations.open_world,
            ),
        )
        self.assertEqual(_annotations(tools["repo_stage"]), (False, True, False, False))
        self.assertEqual(_annotations(tools["repo_commit"]), (False, True, False, False))

    async def test_repo_stage_input_schema_is_closed_and_exact(self) -> None:
        schema = (await self._tools())["repo_stage"].input_schema
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            set(schema["properties"]),
            {"cwd", "branch", "expected_head_sha", "items"},
        )
        self.assertEqual(
            set(schema["required"]),
            {"cwd", "branch", "expected_head_sha", "items"},
        )
        props = schema["properties"]
        self.assertEqual(props["branch"]["maxLength"], 255)
        self.assertEqual(props["expected_head_sha"]["pattern"], "^[0-9a-f]{40}$")
        self.assertEqual(props["items"]["minItems"], 1)
        self.assertEqual(props["items"]["maxItems"], 50)
        item = schema["$defs"]["RepoStageItem"]
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(
            set(item["properties"]),
            {"path", "operation", "expected_sha256"},
        )
        self.assertEqual(
            set(item["required"]),
            {"path", "operation", "expected_sha256"},
        )
        self.assertEqual(set(item["properties"]["operation"]["enum"]), {"present", "delete"})

    async def test_repo_commit_input_schema_is_closed_receipt_guarded_and_has_no_identity(self) -> None:
        schema = (await self._tools())["repo_commit"].input_schema
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            set(schema["properties"]),
            {"cwd", "branch", "expected_head_sha", "expected_diff_receipt", "message"},
        )
        self.assertNotIn("author", schema["properties"])
        self.assertNotIn("email", schema["properties"])
        self.assertEqual(
            set(schema["required"]),
            {"cwd", "branch", "expected_head_sha", "expected_diff_receipt", "message"},
        )
        receipt = schema["$defs"]["ReceiptV1Result"]
        self.assertIs(receipt["additionalProperties"], False)
        self.assertEqual(receipt["properties"]["schema_version"]["const"], 1)
        self.assertEqual(receipt["properties"]["kind"]["const"], "repo-diff")
        self.assertEqual(receipt["properties"]["digest"]["pattern"], "^[0-9a-f]{64}$")

    async def test_repo_stage_and_commit_output_schemas_are_closed(self) -> None:
        tools = await self._tools()
        for name in ("repo_stage", "repo_commit"):
            schema = tools[name].output_schema
            self.assertIsNotNone(schema)
            Draft202012Validator.check_schema(schema)
            for node in _walk(schema):
                if node.get("type") == "object" and "properties" in node:
                    self.assertIs(node.get("additionalProperties"), False, (name, node))
        stage = tools["repo_stage"].output_schema
        self.assertEqual(stage["properties"]["post_stage_clean"]["const"], True)
        self.assertEqual(stage["properties"]["network_used"]["const"], False)
        commit = tools["repo_commit"].output_schema
        self.assertEqual(commit["properties"]["post_commit_clean"]["const"], True)
        self.assertEqual(commit["properties"]["network_used"]["const"], False)
        receipt = commit["$defs"]["RepoCommitReceiptResult"]
        self.assertEqual(receipt["properties"]["kind"]["const"], "repo-commit")

    async def test_mcp_staged_receipt_chain_commits_exact_candidate(self) -> None:
        tools = await self._tools()
        parent = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()

        staged = await server.mcp.call_tool(
            "repo_stage",
            {
                "cwd": str(self.repo),
                "branch": "main",
                "expected_head_sha": parent,
                "items": [
                    {
                        "path": "tracked.txt",
                        "operation": "present",
                        "expected_sha256": digest,
                    }
                ],
            },
        )
        self.assertFalse(staged.is_error, staged)
        Draft202012Validator(tools["repo_stage"].output_schema).validate(
            staged.structured_content
        )
        staged_payload = staged.structured_content
        self.assertTrue(staged_payload["post_stage_clean"])
        self.assertEqual(staged_payload["staged_diff_receipt"]["kind"], "repo-diff")

        committed = await server.mcp.call_tool(
            "repo_commit",
            {
                "cwd": str(self.repo),
                "branch": "main",
                "expected_head_sha": parent,
                "expected_diff_receipt": staged_payload["staged_diff_receipt"],
                "message": "mcp chain",
            },
        )
        self.assertFalse(committed.is_error, committed)
        Draft202012Validator(tools["repo_commit"].output_schema).validate(
            committed.structured_content
        )
        payload = committed.structured_content
        self.assertEqual(payload["parent_sha"], parent)
        self.assertEqual(payload["diff_receipt"], staged_payload["staged_diff_receipt"])
        self.assertEqual(payload["commit_receipt"]["kind"], "repo-commit")
        self.assertTrue(payload["post_commit_clean"])
        self.assertFalse(payload["network_used"])
        self.assertEqual(self._head(), payload["commit_sha"])
        self.assertEqual(_git(self.repo, "status", "--porcelain").stdout, b"")

    async def test_repo_stage_failure_uses_exact_capability_error_envelope(self) -> None:
        parent = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        result = await server.mcp.call_tool(
            "repo_stage",
            {
                "cwd": str(self.repo),
                "branch": "main",
                "expected_head_sha": parent,
                "items": [
                    {
                        "path": "tracked.txt",
                        "operation": "present",
                        "expected_sha256": "0" * 64,
                    }
                ],
            },
        )
        self.assertTrue(result.is_error)
        self.assertEqual(set(result.structured_content), {"error"})
        error = result.structured_content["error"]
        self.assertEqual(set(error), {"code", "reason_code", "message", "retryable", "effect_state", "reconciliation_required", "safe_next_action"})
        self.assertEqual(error["reason_code"], "EXPECTED_SHA_MISMATCH")

    async def test_repo_commit_receipt_mismatch_is_non_mutating_error(self) -> None:
        parent = self._head()
        target = self.repo / "tracked.txt"
        target.write_text("two\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        staged = await server.mcp.call_tool(
            "repo_stage",
            {
                "cwd": str(self.repo),
                "branch": "main",
                "expected_head_sha": parent,
                "items": [
                    {
                        "path": "tracked.txt",
                        "operation": "present",
                        "expected_sha256": digest,
                    }
                ],
            },
        )
        wrong = dict(staged.structured_content["staged_diff_receipt"])
        wrong["digest"] = "0" * 64
        result = await server.mcp.call_tool(
            "repo_commit",
            {
                "cwd": str(self.repo),
                "branch": "main",
                "expected_head_sha": parent,
                "expected_diff_receipt": wrong,
                "message": "must fail",
            },
        )
        self.assertTrue(result.is_error)
        self.assertEqual(
            result.structured_content["error"]["reason_code"],
            "DIFF_RECEIPT_MISMATCH",
        )
        self.assertEqual(self._head(), parent)

    async def test_screen_capture_governance_guard_remains_before_native_capture(self) -> None:
        with patch(
            "agent_runtime.server.capture_screen",
            side_effect=AssertionError("native capture must not run"),
        ):
            result = await server.mcp.call_tool("screen_capture", {})
        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(error["reason_code"], "VISUAL_PERCEPTION_BLOCKED")
        self.assertEqual(error["effect_state"], "absent")
        self.assertEqual(error["safe_next_action"], "unsupported")


if __name__ == "__main__":
    unittest.main()
