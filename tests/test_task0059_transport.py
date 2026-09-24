from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import TextContent

ROOT = Path(__file__).resolve().parents[1]


def _result_text(result: object) -> str:
    return "\n".join(
        block.text
        for block in getattr(result, "content")
        if isinstance(block, TextContent)
    )


def _stdio_parameters(root: Path) -> StdioServerParameters:
    home = root / "home"
    temp = root / "tmp"
    home.mkdir()
    temp.mkdir()
    return StdioServerParameters(
        command=sys.executable,
        args=["-u", "-m", "agent_runtime.server"],
        cwd=str(ROOT),
        env={
            "AGENT_RUNTIME_WORKSPACE_ROOT": str(root),
            "AGENT_RUNTIME_MAX_PARALLELISM": "6",
            "HOME": str(home),
            "TMPDIR": str(temp),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


async def _wait_for_paths(paths: list[Path]) -> None:
    """Wait for test-owned child barriers; the timeout is only a hang guard."""

    for _ in range(1000):
        if all(path.exists() for path in paths):
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"child barrier did not open: {paths!r}")


def _held_workload() -> str:
    return r"""
import fcntl
import pathlib
import sys
import time

token, ready, release, counter = sys.argv[1:]
counter_path = pathlib.Path(counter)

def update(delta):
    counter_path.touch()
    with counter_path.open("r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        raw = handle.read().strip()
        active, peak = (map(int, raw.split(",")) if raw else (0, 0))
        active += delta
        peak = max(peak, active)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{active},{peak}")
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

update(1)
try:
    pathlib.Path(ready).write_text(token)
    while not pathlib.Path(release).exists():
        time.sleep(0.005)
finally:
    update(-1)
print(token, flush=True)
"""


class SharedMCPTransportTests(unittest.IsolatedAsyncioTestCase):
    """Evidence for one isolated MCP stdio child and the accepted x6 boundary."""

    async def test_one_shared_stdio_child_correlates_eight_mixed_callers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-runtime-task0059-mixed-") as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            release = workspace / "release"
            ready = [workspace / f"terminal-ready-{index}" for index in range(4)]
            files = []
            for index in range(4):
                marker = f"TASK0059_FS_CALLER_{index}_RESULT"
                path = workspace / f"read-{index}.txt"
                path.write_text(marker + "\n", encoding="utf-8")
                files.append((path, marker))

            terminal_code = (
                "import pathlib, sys, time\n"
                "token, ready, release = sys.argv[1:]\n"
                "pathlib.Path(ready).write_text(token)\n"
                "while not pathlib.Path(release).exists(): time.sleep(0.005)\n"
                "print(token, flush=True)\n"
            )

            async with Client(_stdio_parameters(root), read_timeout_seconds=10) as client:
                async def request(index: int):
                    if index % 2 == 0:
                        terminal_index = index // 2
                        token = f"TASK0059_EXEC_CALLER_{terminal_index}_RESULT"
                        return index, await client.call_tool(
                            "terminal_exec",
                            {
                                "argv": [
                                    sys.executable,
                                    "-u",
                                    "-c",
                                    terminal_code,
                                    token,
                                    str(ready[terminal_index]),
                                    str(release),
                                ],
                                "cwd": str(workspace),
                                "start_identity": f"{terminal_index + 1:032x}",
                                "timeout_seconds": 10,
                            },
                        )

                    file_index = index // 2
                    return index, await client.call_tool(
                        "fs_read_batch",
                        {
                            "cwd": str(workspace),
                            "items": [{"path": files[file_index][0].name}],
                        },
                    )

                tasks = [asyncio.create_task(request(index)) for index in range(8)]
                await _wait_for_paths(ready)
                release.write_text("release", encoding="utf-8")
                results = await asyncio.gather(*tasks)

            for index, result in results:
                self.assertFalse(result.is_error, (index, _result_text(result)))
                if index % 2 == 0:
                    terminal_index = index // 2
                    token = f"TASK0059_EXEC_CALLER_{terminal_index}_RESULT"
                    payload = result.structured_content
                    self.assertEqual(payload["argv"][-3], token)
                    self.assertEqual(payload["stdout"], token + "\n")
                    self.assertEqual(payload["exit_code"], 0)
                else:
                    file_index = index // 2
                    payload = result.structured_content
                    self.assertEqual(payload["items"][0]["text"], files[file_index][1] + "\n")

    async def test_shared_stdio_holds_six_heavy_rejects_seventh_and_keeps_cheap_calls_live(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-runtime-task0059-x6-") as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            ready = [workspace / f"heavy-ready-{index}" for index in range(6)]
            release = workspace / "heavy-release"
            spawned = workspace / "heavy-counter"
            cheap = workspace / "cheap.txt"
            cheap.write_text("TASK0059_CHEAP_READ_RESULT\n", encoding="utf-8")
            code = _held_workload()

            async with Client(_stdio_parameters(root), read_timeout_seconds=10) as client:
                heavy_tasks = [
                    asyncio.create_task(
                        client.call_tool(
                            "terminal_exec",
                            {
                                "argv": [
                                    sys.executable,
                                    "-u",
                                    "-c",
                                    code,
                                    f"TASK0059_HEAVY_{index}",
                                    str(ready[index]),
                                    str(release),
                                    str(spawned),
                                ],
                                "cwd": str(workspace),
                                "start_identity": f"{index + 1:032x}",
                                "timeout_seconds": 10,
                            },
                        )
                    )
                    for index in range(6)
                ]
                try:
                    await _wait_for_paths(ready)
                    active, peak = map(int, spawned.read_text().split(","))
                    self.assertEqual(active, 6)
                    self.assertEqual(peak, 6)

                    capacity_result, read_result = await asyncio.wait_for(
                        asyncio.gather(
                            client.call_tool("capacity_observer", {}),
                            client.call_tool(
                                "fs_read_batch",
                                {
                                    "cwd": str(workspace),
                                    "items": [{"path": cheap.name}],
                                },
                            ),
                        ),
                        timeout=5,
                    )
                    self.assertFalse(capacity_result.is_error, _result_text(capacity_result))
                    self.assertIsNotNone(capacity_result.structured_content)
                    self.assertFalse(read_result.is_error, _result_text(read_result))
                    self.assertEqual(
                        read_result.structured_content["items"][0]["text"],
                        "TASK0059_CHEAP_READ_RESULT\n",
                    )

                    seventh = await client.call_tool(
                        "terminal_exec",
                        {
                            "argv": [
                                sys.executable,
                                "-u",
                                "-c",
                                "import pathlib; pathlib.Path(__import__('sys').argv[1]).write_text('spawned')",
                                str(workspace / "seventh-spawned"),
                            ],
                            "cwd": str(workspace),
                            "start_identity": f"{7:032x}",
                            "timeout_seconds": 10,
                        },
                    )
                    self.assertTrue(seventh.is_error)
                    error = seventh.structured_content["error"]
                    self.assertEqual(error["code"], "LIMIT_EXCEEDED")
                    self.assertEqual(error["reason_code"], "CAPACITY_EXHAUSTED")
                    self.assertEqual(error["effect_state"], "absent")
                    self.assertFalse(error["reconciliation_required"])
                    self.assertEqual(error["safe_next_action"], "wait")
                    self.assertFalse((workspace / "seventh-spawned").exists())
                    active, peak = map(int, spawned.read_text().split(","))
                    self.assertEqual(active, 6)
                    self.assertEqual(peak, 6)
                finally:
                    release.write_text("release", encoding="utf-8")
                    results = await asyncio.gather(*heavy_tasks, return_exceptions=True)

                for index, result in enumerate(results):
                    self.assertFalse(isinstance(result, BaseException), (index, result))
                    self.assertFalse(result.is_error, (index, _result_text(result)))
                    self.assertEqual(result.structured_content["exit_code"], 0)
                    self.assertEqual(
                        result.structured_content["stdout"],
                        f"TASK0059_HEAVY_{index}\n",
                    )

            active, peak = map(int, spawned.read_text().split(","))
            self.assertEqual(active, 0)
            self.assertEqual(peak, 6)


if __name__ == "__main__":
    unittest.main()
