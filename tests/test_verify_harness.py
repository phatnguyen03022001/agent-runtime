from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import verify_tests

ROOT = Path(__file__).resolve().parents[1]


class VerifyHarnessTests(unittest.TestCase):
    def _fixture_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        shutil.copy2(ROOT / "verify", repo / "verify")
        shutil.copy2(ROOT / "verify_tests.py", repo / "verify_tests.py")
        (repo / "verify").chmod(0o700)

        for name in ("install.sh", "start.sh"):
            path = repo / name
            path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")
            path.chmod(0o700)

        package = repo / "agent_runtime"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

        tests = repo / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        return repo

    def _write_test(self, repo: Path, name: str, body: str) -> None:
        (repo / "tests" / name).write_text(body, encoding="utf-8")

    def _run_verify(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHON"] = sys.executable
        return subprocess.run(
            [str(repo / "verify"), *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_classification_is_explicit_unknown_defaults_serial_and_workers_are_fixed_x4(self) -> None:
        fast, serial = verify_tests.classify_modules(
            ["tests.test_capability_registry", "tests.test_new_unknown"]
        )
        self.assertEqual(fast, ["tests.test_capability_registry"])
        self.assertEqual(serial, ["tests.test_new_unknown"])
        self.assertEqual(verify_tests.WORKER_COUNT, 4)

        shards = verify_tests.build_fast_shards(fast)
        self.assertEqual(len(shards), 4)
        self.assertEqual(
            [module for shard in shards for module in shard],
            ["tests.test_capability_registry"],
        )

    def test_serial_reload_boundary_preserves_order_without_parallelism(self) -> None:
        modules = [
            "tests.test_surface_and_scripts",
            "tests.test_task0041_boundedness",
            "tests.test_tunnel_identity",
        ]
        self.assertEqual(
            verify_tests.serial_worker_groups(modules),
            [
                ["tests.test_surface_and_scripts"],
                ["tests.test_task0041_boundedness", "tests.test_tunnel_identity"],
            ],
        )

    def test_quick_runs_only_proven_safe_lane(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self._fixture_repo(Path(raw))
            self._write_test(
                repo,
                "test_capability_registry.py",
                """
import unittest
from pathlib import Path

class SafeFixture(unittest.TestCase):
    def test_safe(self):
        Path("safe-ran").write_text("yes")
""",
            )
            self._write_test(
                repo,
                "test_unknown_serial.py",
                """
import unittest
from pathlib import Path

class UnknownFixture(unittest.TestCase):
    def test_unknown(self):
        Path("serial-ran").write_text("yes")
        self.fail("serial lane must not run in quick mode")
""",
            )

            result = self._run_verify(repo, "--quick")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / "safe-ran").is_file())
            self.assertFalse((repo / "serial-ran").exists())
            self.assertIn("lane=FAST_PARALLEL_SAFE", result.stdout)
            self.assertIn("mode=quick", result.stdout)
            self.assertNotIn("module=tests.test_unknown_serial", result.stdout)

    def test_parallel_worker_failure_propagates_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self._fixture_repo(Path(raw))
            self._write_test(
                repo,
                "test_capability_registry.py",
                """
import unittest

class WorkerFailureFixture(unittest.TestCase):
    def test_failure(self):
        self.fail("worker failure sentinel")
""",
            )

            result = self._run_verify(repo, "--quick")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "VERIFY failure module=tests.test_capability_registry",
                result.stderr,
            )
            self.assertIn("worker failure sentinel", result.stderr)

    def test_serial_failure_propagates_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self._fixture_repo(Path(raw))
            self._write_test(
                repo,
                "test_unknown_serial.py",
                """
import unittest

class SerialFailureFixture(unittest.TestCase):
    def test_failure(self):
        self.fail("serial failure sentinel")
""",
            )

            result = self._run_verify(repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "VERIFY failure module=tests.test_unknown_serial",
                result.stderr,
            )
            self.assertIn("serial failure sentinel", result.stderr)

    def test_python_syntax_failure_stops_before_test_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self._fixture_repo(Path(raw))
            (repo / "agent_runtime" / "broken.py").write_text(
                "def broken(:\n",
                encoding="utf-8",
            )
            self._write_test(
                repo,
                "test_capability_registry.py",
                """
import unittest
from pathlib import Path

class MustNotRunFixture(unittest.TestCase):
    def test_marker(self):
        Path("test-ran").write_text("yes")
""",
            )

            result = self._run_verify(repo, "--quick")

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((repo / "test-ran").exists())
            self.assertNotIn("VERIFY module=", result.stdout)

    def test_shell_syntax_failure_stops_before_test_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self._fixture_repo(Path(raw))
            (repo / "start.sh").write_text("if; then\n", encoding="utf-8")
            self._write_test(
                repo,
                "test_capability_registry.py",
                """
import unittest
from pathlib import Path

class MustNotRunFixture(unittest.TestCase):
    def test_marker(self):
        Path("test-ran").write_text("yes")
""",
            )

            result = self._run_verify(repo, "--quick")

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((repo / "test-ran").exists())
            self.assertNotIn("VERIFY module=", result.stdout)


if __name__ == "__main__":
    unittest.main()
