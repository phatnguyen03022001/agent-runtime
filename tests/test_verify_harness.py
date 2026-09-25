from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

import verification_policy
import verify_tests

ROOT = Path(__file__).resolve().parents[1]


class RecordingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.flush_count = 0

    def write(self, text: str) -> int:
        with self._lock:
            return super().write(text)

    def flush(self) -> None:
        with self._lock:
            self.flush_count += 1
            super().flush()

    def snapshot(self) -> str:
        with self._lock:
            return super().getvalue()


class VerifyHarnessTests(unittest.TestCase):
    BASE = "a" * 40
    HEAD = "b" * 40

    def _fixture_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        for name in ("verify", "verify_tests.py", "verification_policy.py"):
            shutil.copy2(ROOT / name, repo / name)
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

    def _run_verify(
        self,
        repo: Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
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

    def _synthetic_root(self, root: Path) -> Path:
        package = root / "fixturemods"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        return package

    def _write_synthetic_module(
        self,
        root: Path,
        name: str,
        body: str,
    ) -> str:
        package = root / "fixturemods"
        (package / f"{name}.py").write_text(body, encoding="utf-8")
        return f"fixturemods.{name}"

    def _selected(
        self,
        module: str,
        *,
        lane: str = verification_policy.L2_ISOLATED_INTEGRATION,
        isolation_key: str | None = None,
        timeout: float = 2.0,
    ) -> verify_tests.SelectedModule:
        return verify_tests.SelectedModule(
            module=module,
            policy=verification_policy.ModulePolicy(
                lane=lane,
                subsystem_tags=("harness-fixture",),
                isolation_key=isolation_key,
                proof_rationale="Synthetic verification-harness fixture.",
                timeout_seconds=timeout,
            ),
            reasons=("harness-test",),
        )

    def test_inventory_is_classified_exactly_once_with_expected_lane_counts(self) -> None:
        modules = verify_tests.discover_modules()
        verify_tests.validate_classification(modules)
        self.assertEqual(len(modules), 62)
        self.assertEqual(set(modules), set(verification_policy.MODULE_POLICIES))
        counts = Counter(
            policy.lane
            for policy in verification_policy.MODULE_POLICIES.values()
        )
        self.assertEqual(
            counts,
            {
                verification_policy.L1_DETERMINISTIC_UNIT: 17,
                verification_policy.L2_ISOLATED_INTEGRATION: 21,
                verification_policy.L3_DETERMINISTIC_REGRESSION: 10,
                verification_policy.L4_HOST_LIFECYCLE: 9,
                verification_policy.L5_QUALIFICATION_CHAOS_CUTOVER: 5,
            },
        )
        self.assertEqual(verification_policy.GLOBAL_WORKER_LIMIT, 4)
        self.assertLessEqual(verification_policy.HEARTBEAT_SECONDS, 5.0)

    def test_unknown_test_module_fails_closed(self) -> None:
        modules = verify_tests.discover_modules() + ["tests.test_new_unknown"]
        with self.assertRaisesRegex(ValueError, "unclassified=tests.test_new_unknown"):
            verify_tests.validate_classification(modules)

    def test_duplicate_classification_fails_closed(self) -> None:
        policy = verification_policy.ModulePolicy(
            lane=verification_policy.L1_DETERMINISTIC_UNIT,
            subsystem_tags=("fixture",),
            isolation_key=None,
            proof_rationale="fixture",
            timeout_seconds=1.0,
        )
        with self.assertRaisesRegex(
            ValueError,
            "duplicate verification classification: tests.test_duplicate",
        ):
            verification_policy.build_policy_map(
                (
                    ("tests.test_duplicate", policy),
                    ("tests.test_duplicate", policy),
                )
            )

    def test_unknown_changed_path_escalates_to_qualification(self) -> None:
        plan = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_CANDIDATE,
            base=self.BASE,
            head=self.HEAD,
            paths=("unknown/runtime.surface",),
        )
        self.assertEqual(
            plan.effective_profile,
            verify_tests.PROFILE_QUALIFICATION,
        )
        self.assertIn("unknown changed path: unknown/runtime.surface", plan.escalations)
        self.assertEqual(
            {item.module for item in plan.selected},
            set(verification_policy.MODULE_POLICIES),
        )

    def test_verification_infrastructure_change_forces_qualification(self) -> None:
        for path in (
            "verify",
            "verify_tests.py",
            "verification_policy.py",
            "tests/test_verify_harness.py",
        ):
            with self.subTest(path=path):
                plan = verify_tests.build_plan(
                    requested_profile=verify_tests.PROFILE_QUICK,
                    base=self.BASE,
                    head=self.HEAD,
                    paths=(path,),
                )
                self.assertEqual(
                    plan.effective_profile,
                    verify_tests.PROFILE_QUALIFICATION,
                )
                self.assertEqual(
                    {item.module for item in plan.selected},
                    set(verification_policy.MODULE_POLICIES),
                )

    def test_repo_change_has_deterministic_quick_and_candidate_selection(self) -> None:
        path = "agent_runtime/repo_commit.py"
        quick = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_QUICK,
            base=self.BASE,
            head=self.HEAD,
            paths=(path,),
        )
        candidate = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_CANDIDATE,
            base=self.BASE,
            head=self.HEAD,
            paths=(path,),
        )
        self.assertEqual(quick.effective_profile, verify_tests.PROFILE_QUICK)
        self.assertTrue(
            set(verification_policy.GLOBAL_QUICK_MODULES).issubset(
                {item.module for item in quick.selected}
            )
        )
        self.assertTrue(
            any(item.module == "tests.test_repo_commit" for item in quick.selected)
        )
        self.assertTrue(
            all(
                item.policy.lane
                in {
                    verification_policy.L1_DETERMINISTIC_UNIT,
                    verification_policy.L2_ISOLATED_INTEGRATION,
                }
                for item in quick.selected
            )
        )

        expected_candidate = {
            module
            for module, policy in verification_policy.MODULE_POLICIES.items()
            if policy.lane
            in {
                verification_policy.L1_DETERMINISTIC_UNIT,
                verification_policy.L2_ISOLATED_INTEGRATION,
                verification_policy.L3_DETERMINISTIC_REGRESSION,
            }
        }
        self.assertEqual(
            {item.module for item in candidate.selected},
            expected_candidate,
        )
        self.assertFalse(
            any(
                item.policy.lane
                in {
                    verification_policy.L4_HOST_LIFECYCLE,
                    verification_policy.L5_QUALIFICATION_CHAOS_CUTOVER,
                }
                for item in candidate.selected
            )
        )

    def test_terminal_lifecycle_change_triggers_lifecycle_and_restart_proof(self) -> None:
        plan = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_CANDIDATE,
            base=self.BASE,
            head=self.HEAD,
            paths=("agent_runtime/session.py",),
        )
        names = {item.module for item in plan.selected}
        self.assertIn("tests.test_terminal_exec", names)
        self.assertIn("tests.test_terminal_session", names)
        self.assertIn("tests.test_supervised_lifecycle", names)
        self.assertIn("tests.test_durable_pipe_restart", names)
        self.assertEqual(plan.effective_profile, verify_tests.PROFILE_CANDIDATE)

    def test_direct_high_cost_test_change_escalates_to_qualification(self) -> None:
        plan = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_CANDIDATE,
            base=self.BASE,
            head=self.HEAD,
            paths=("tests/test_tunnel_identity.py",),
        )
        self.assertEqual(
            plan.effective_profile,
            verify_tests.PROFILE_QUALIFICATION,
        )
        self.assertTrue(
            any("high-cost proof module changed directly" in item for item in plan.escalations)
        )

    def test_docs_only_change_selects_l0_only_for_quick_and_candidate(self) -> None:
        for profile in (
            verify_tests.PROFILE_QUICK,
            verify_tests.PROFILE_CANDIDATE,
        ):
            with self.subTest(profile=profile):
                plan = verify_tests.build_plan(
                    requested_profile=profile,
                    base=self.BASE,
                    head=self.HEAD,
                    paths=("README.md",),
                )
                self.assertTrue(plan.docs_only)
                self.assertEqual(plan.effective_profile, profile)
                self.assertEqual(plan.selected, ())

    def test_same_inputs_produce_byte_stable_ordered_plan(self) -> None:
        first = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_CANDIDATE,
            base=self.BASE,
            head=self.HEAD,
            paths=("agent_runtime/repo_commit.py", "agent_runtime/repo_diff.py"),
        )
        second = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_CANDIDATE,
            base=self.BASE,
            head=self.HEAD,
            paths=("agent_runtime/repo_diff.py", "agent_runtime/repo_commit.py"),
        )
        first_bytes = verify_tests.serialize_plan(first).encode()
        second_bytes = verify_tests.serialize_plan(second).encode()
        self.assertEqual(first_bytes, second_bytes)
        payload = verify_tests.plan_payload(first)
        modules = [
            item["module"]
            for item in payload["selected_modules"]
        ]
        self.assertEqual(modules, sorted(modules))

    def test_qualification_includes_every_classified_module(self) -> None:
        plan = verify_tests.build_plan(
            requested_profile=verify_tests.PROFILE_QUALIFICATION,
            base=self.BASE,
            head=self.HEAD,
            paths=(),
        )
        self.assertEqual(
            {item.module for item in plan.selected},
            set(verification_policy.MODULE_POLICIES),
        )

    def test_isolation_key_conflicts_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            marker = root / "markers"
            marker.mkdir()
            module_a = self._write_synthetic_module(
                root,
                "conflict_a",
                """
import os
import time
import unittest
from pathlib import Path

class ConflictA(unittest.TestCase):
    def test_serialized(self):
        root = Path(os.environ["VERIFY_FIXTURE_ROOT"])
        own = root / "a.running"
        other = root / "b.running"
        self.assertFalse(other.exists())
        own.write_text("a")
        try:
            time.sleep(0.20)
            self.assertFalse(other.exists())
        finally:
            own.unlink(missing_ok=True)
""",
            )
            module_b = self._write_synthetic_module(
                root,
                "conflict_b",
                """
import os
import time
import unittest
from pathlib import Path

class ConflictB(unittest.TestCase):
    def test_serialized(self):
        root = Path(os.environ["VERIFY_FIXTURE_ROOT"])
        own = root / "b.running"
        other = root / "a.running"
        self.assertFalse(other.exists())
        own.write_text("b")
        try:
            time.sleep(0.20)
            self.assertFalse(other.exists())
        finally:
            own.unlink(missing_ok=True)
""",
            )
            outcome = verify_tests.execute_selected(
                (
                    self._selected(module_a, isolation_key="shared"),
                    self._selected(module_b, isolation_key="shared"),
                ),
                profile="harness",
                root=root,
                worker_script=ROOT / "verify_tests.py",
                extra_env={"VERIFY_FIXTURE_ROOT": str(marker)},
                global_worker_limit=2,
                heartbeat_seconds=0.5,
            )
            self.assertTrue(outcome.success)
            self.assertEqual(len(outcome.results), 2)

    def test_parallel_safe_independent_modules_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            marker = root / "markers"
            marker.mkdir()
            module_a = self._write_synthetic_module(
                root,
                "parallel_a",
                """
import os
import time
import unittest
from pathlib import Path

class ParallelA(unittest.TestCase):
    def test_overlap(self):
        root = Path(os.environ["VERIFY_FIXTURE_ROOT"])
        own = root / "a.ready"
        other = root / "b.ready"
        own.write_text("a")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not other.exists():
            time.sleep(0.01)
        self.assertTrue(other.exists())
""",
            )
            module_b = self._write_synthetic_module(
                root,
                "parallel_b",
                """
import os
import time
import unittest
from pathlib import Path

class ParallelB(unittest.TestCase):
    def test_overlap(self):
        root = Path(os.environ["VERIFY_FIXTURE_ROOT"])
        own = root / "b.ready"
        other = root / "a.ready"
        own.write_text("b")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not other.exists():
            time.sleep(0.01)
        self.assertTrue(other.exists())
""",
            )
            outcome = verify_tests.execute_selected(
                (
                    self._selected(module_a),
                    self._selected(module_b),
                ),
                profile="harness",
                root=root,
                worker_script=ROOT / "verify_tests.py",
                extra_env={"VERIFY_FIXTURE_ROOT": str(marker)},
                global_worker_limit=2,
                heartbeat_seconds=0.5,
            )
            self.assertTrue(outcome.success)
            self.assertEqual(len(outcome.results), 2)

    def test_module_timeout_kills_only_owned_worker_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            marker = root / "markers"
            marker.mkdir()
            module = self._write_synthetic_module(
                root,
                "timeout_case",
                """
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

class TimeoutCase(unittest.TestCase):
    def test_timeout(self):
        root = Path(os.environ["VERIFY_FIXTURE_ROOT"])
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        (root / "child.pid").write_text(str(child.pid))
        time.sleep(30)
""",
            )
            sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            err = RecordingStream()
            try:
                outcome = verify_tests.execute_selected(
                    (self._selected(module, timeout=0.4),),
                    profile="harness",
                    root=root,
                    worker_script=ROOT / "verify_tests.py",
                    extra_env={"VERIFY_FIXTURE_ROOT": str(marker)},
                    err=err,
                    heartbeat_seconds=0.1,
                )
                self.assertFalse(outcome.success)
                self.assertIn(
                    "VERIFY TIMEOUT lane=L2_ISOLATED_INTEGRATION module=fixturemods.timeout_case",
                    err.snapshot(),
                )
                self.assertIsNone(sentinel.poll(), "unrelated process must remain alive")
                child_pid_path = marker / "child.pid"
                self.assertTrue(child_pid_path.is_file())
                child_pid = int(child_pid_path.read_text())
                deadline = time.monotonic() + 1.5
                child_alive = True
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        child_alive = False
                        break
                    time.sleep(0.02)
                self.assertFalse(child_alive, "worker process-group child survived timeout")
            finally:
                if sentinel.poll() is None:
                    sentinel.terminate()
                    try:
                        sentinel.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        sentinel.kill()
                        sentinel.wait(timeout=1)

    def test_worker_crash_without_result_fails_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            module = self._write_synthetic_module(
                root,
                "crash_case",
                """
import os
import unittest

class CrashCase(unittest.TestCase):
    def test_crash(self):
        os._exit(7)
""",
            )
            err = RecordingStream()
            outcome = verify_tests.execute_selected(
                (self._selected(module),),
                profile="harness",
                root=root,
                worker_script=ROOT / "verify_tests.py",
                err=err,
                heartbeat_seconds=0.2,
            )
            self.assertFalse(outcome.success)
            self.assertIn("reason=worker-missing-result exit_code=7", err.snapshot())

    def test_failure_propagation_is_exact_and_visible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            module = self._write_synthetic_module(
                root,
                "failure_case",
                """
import unittest

class FailureCase(unittest.TestCase):
    def test_failure(self):
        self.fail("worker failure sentinel")
""",
            )
            err = RecordingStream()
            outcome = verify_tests.execute_selected(
                (self._selected(module),),
                profile="harness",
                root=root,
                worker_script=ROOT / "verify_tests.py",
                err=err,
                heartbeat_seconds=0.2,
            )
            self.assertFalse(outcome.success)
            text = err.snapshot()
            self.assertIn(
                "VERIFY FAIL lane=L2_ISOLATED_INTEGRATION module=fixturemods.failure_case",
                text,
            )
            self.assertIn("worker failure sentinel", text)
            self.assertGreaterEqual(err.flush_count, 2)

    def test_start_and_pass_events_are_flushed_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            module = self._write_synthetic_module(
                root,
                "pass_case",
                """
import unittest

class PassCase(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(2 + 2, 4)
""",
            )
            out = RecordingStream()
            outcome = verify_tests.execute_selected(
                (self._selected(module),),
                profile="harness",
                root=root,
                worker_script=ROOT / "verify_tests.py",
                out=out,
                heartbeat_seconds=0.2,
            )
            self.assertTrue(outcome.success)
            text = out.snapshot()
            self.assertLess(text.index("VERIFY START"), text.index("VERIFY PASS"))
            self.assertGreaterEqual(out.flush_count, 2)

    def test_heartbeat_is_emitted_before_slow_fixture_completes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._synthetic_root(root)
            module = self._write_synthetic_module(
                root,
                "slow_case",
                """
import time
import unittest

class SlowCase(unittest.TestCase):
    def test_slow(self):
        time.sleep(0.50)
""",
            )
            out = RecordingStream()
            holder: list[verify_tests.VerificationOutcome] = []

            def run() -> None:
                holder.append(
                    verify_tests.execute_selected(
                        (self._selected(module, timeout=2.0),),
                        profile="harness",
                        root=root,
                        worker_script=ROOT / "verify_tests.py",
                        out=out,
                        heartbeat_seconds=0.10,
                    )
                )

            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 1.0
            observed_while_running = False
            while time.monotonic() < deadline:
                if "VERIFY HEARTBEAT" in out.snapshot():
                    observed_while_running = thread.is_alive()
                    break
                time.sleep(0.01)
            thread.join(timeout=2.0)
            self.assertTrue(observed_while_running)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(holder), 1)
            self.assertTrue(holder[0].success)
            self.assertIn("VERIFY PASS", out.snapshot())

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

            result = self._run_verify(repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((repo / "test-ran").exists())
            self.assertIn("SyntaxError", result.stderr)
            self.assertNotIn("VERIFY START", result.stdout)

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

            result = self._run_verify(repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((repo / "test-ran").exists())
            self.assertIn("syntax error", result.stderr.lower())
            self.assertNotIn("VERIFY START", result.stdout)


if __name__ == "__main__":
    unittest.main()
