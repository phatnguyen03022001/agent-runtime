from __future__ import annotations

import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_runtime.executor import execute_terminal


class PressureConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name) / "workspace"
        self.cwd.mkdir()
        self.previous_root = os.environ.get("AGENT_RUNTIME_WORKSPACE_ROOT")
        os.environ["AGENT_RUNTIME_WORKSPACE_ROOT"] = str(self.cwd)

    def tearDown(self) -> None:
        if self.previous_root is None:
            os.environ.pop("AGENT_RUNTIME_WORKSPACE_ROOT", None)
        else:
            os.environ["AGENT_RUNTIME_WORKSPACE_ROOT"] = self.previous_root
        self.temp.cleanup()

    def test_twenty_independent_terminal_exec_operations_complete_without_lifecycle_serialization(self) -> None:
        def run(index: int) -> tuple[int, str]:
            result = execute_terminal(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "print('exec-' + __import__('sys').argv[1], flush=True)",
                    str(index),
                ],
                str(self.cwd),
                timeout_seconds=10,
            )
            return result["exit_code"], result["stdout"]

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(run, range(20)))

        self.assertEqual([code for code, _output in results], [0] * 20)
        self.assertEqual(
            {output.strip() for _code, output in results},
            {f"exec-{index}" for index in range(20)},
        )


if __name__ == "__main__":
    unittest.main()
