from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from macos.recover_runtime_service import (
    LABEL,
    LaunchdSnapshot,
    ProcessSnapshot,
    RecoveryError,
    RuntimeObservation,
    RuntimeServiceRecovery,
    tunnel_fingerprint,
)


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_ctx = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_ctx.name).resolve()
        self.home = self.temp / "home"
        self.repo = self.temp / "candidate"
        self.canonical = self.temp / "agent-runtime"
        for path in (self.home, self.repo, self.canonical):
            path.mkdir()
        for root in (self.repo, self.canonical):
            (root / "start.sh").write_text("#!/bin/sh\nexit 0\n")
            (root / "start.sh").chmod(0o700)
        (self.canonical / ".env").write_text(
            f"CONTROL_PLANE_API_KEY=dummy\nCONTROL_PLANE_TUNNEL_ID=stable-fixture-id\nAGENT_RUNTIME_WORKSPACE_ROOT={self.canonical.parent}\n"
        )
        venv_target = self.temp / "shared-venv"
        venv_target.mkdir()
        (self.canonical / ".venv").symlink_to(venv_target, target_is_directory=True)
        self.expected = tunnel_fingerprint("stable-fixture-id")

    def tearDown(self) -> None:
        self.temp_ctx.cleanup()

    def recovery(self) -> RuntimeServiceRecovery:
        return RuntimeServiceRecovery(
            self.repo, self.canonical, self.home, self.expected
        )

    def stale_job(self, *, state: str = "not running") -> LaunchdSnapshot:
        root = "/private/tmp/tmpfixture123"
        return LaunchdSnapshot(
            f"{root}/home/Library/LaunchAgents/{LABEL}.plist",
            state,
            f"{root}/agent-runtime/start.sh",
        )

    def canonical_job(self) -> LaunchdSnapshot:
        return LaunchdSnapshot(
            str(self.home / "Library/LaunchAgents" / f"{LABEL}.plist"),
            "not running",
            str(self.canonical / "start.sh"),
        )

    def canonical_processes(self, *, wrong_identity: bool = False):
        runtime_python = self.temp / "wrong-python" if wrong_identity else self.canonical / ".venv/bin/python"
        tunnel = ProcessSnapshot(
            100, 10, 100, "/opt/homebrew/bin/tunnel-client",
            "/opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main "
            f"--mcp.command command={runtime_python} -m agent_runtime.server,channel=main "
            "--health.listen-addr 127.0.0.1:8080",
        )
        child = ProcessSnapshot(
            101, 100, 100, str(runtime_python),
            f"{runtime_python} -m agent_runtime.server",
        )
        return tunnel, child

    def stale_observation(self, **changes) -> RuntimeObservation:
        tunnel, child = self.canonical_processes()
        values = dict(
            desired_running=False,
            tunnel_fingerprint=self.expected,
            launchd=self.stale_job(),
            listeners=(100,),
            processes=(tunnel, child),
            launchd_environment_ready=False,
        )
        values.update(changes)
        return RuntimeObservation(**values)

    def test_stale_fixture_provenance_is_exact_and_nonserving(self) -> None:
        good = self.stale_job()
        self.assertTrue(RuntimeServiceRecovery.is_proven_stale_fixture(good))
        variants = [
            self.stale_job(state="running"),
            LaunchdSnapshot(
                f"/Users/test/Library/LaunchAgents/{LABEL}.plist",
                "not running", good.program,
            ),
            LaunchdSnapshot(good.path, "not running", "/private/tmp/other/agent-runtime/start.sh"),
            LaunchdSnapshot(good.path.replace(LABEL, "com.example.other"), "not running", good.program),
        ]
        for snapshot in variants:
            with self.subTest(snapshot=snapshot):
                self.assertFalse(RuntimeServiceRecovery.is_proven_stale_fixture(snapshot))

    def test_exact_stale_state_produces_one_bounded_plan(self) -> None:
        plan = self.recovery().plan(self.stale_observation())
        self.assertEqual(plan.kind, "migrate_stale_fixture")
        self.assertEqual(plan.tunnel_pid, 100)
        self.assertEqual(plan.tunnel_pgid, 100)
        self.assertEqual(
            plan.actions,
            (
                "write_canonical_plist",
                "remove_stale_fixture_registration",
                "register_canonical_service",
                "terminate_unsupervised_runtime",
            ),
        )

    def test_running_desired_state_and_serving_job_fail_closed(self) -> None:
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(desired_running=True))
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(launchd=self.stale_job(state="running")))

    def test_foreign_or_wrong_runtime_identity_fails_closed(self) -> None:
        foreign = ProcessSnapshot(
            100, 10, 100, "/usr/bin/python3",
            "/usr/bin/python3 -m http.server 8080",
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(processes=(foreign,)))

        wrong_tunnel, child = self.canonical_processes(wrong_identity=True)
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(processes=(wrong_tunnel, child)))

    def test_multiple_or_wrong_process_tree_fails_closed(self) -> None:
        tunnel, child = self.canonical_processes()
        duplicate = ProcessSnapshot(
            102, 10, 102, tunnel.executable, tunnel.argv
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(
                self.stale_observation(processes=(tunnel, child, duplicate))
            )
        wrong_child = ProcessSnapshot(
            101, 999, 100, child.executable, child.argv
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(
                self.stale_observation(processes=(tunnel, wrong_child))
            )

    def test_unrelated_process_in_runtime_process_group_fails_closed(self) -> None:
        tunnel, child = self.canonical_processes()
        unrelated = ProcessSnapshot(
            103, 100, 100, "/usr/bin/sleep", "/usr/bin/sleep 300"
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(
                self.stale_observation(processes=(tunnel, child, unrelated))
            )

    def test_wrong_tunnel_fingerprint_or_multiple_listeners_fail_closed(self) -> None:
        with self.assertRaises(RecoveryError):
            self.recovery().plan(
                self.stale_observation(tunnel_fingerprint="000000000000")
            )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(listeners=(100, 777)))

    def test_non_temp_or_ambiguous_launchd_provenance_fails_closed(self) -> None:
        canonical_job = LaunchdSnapshot(
            str(self.home / "Library/LaunchAgents" / f"{LABEL}.plist"),
            "not running",
            str(self.canonical / "start.sh"),
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(launchd=canonical_job))
        ambiguous = LaunchdSnapshot(
            self.stale_job().path,
            "not running",
            "/private/tmp/other/agent-runtime/start.sh",
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(launchd=ambiguous))

    def test_converged_stopped_state_is_idempotent_noop(self) -> None:
        canonical_job = LaunchdSnapshot(
            str(self.home / "Library/LaunchAgents" / f"{LABEL}.plist"),
            "not running",
            str(self.canonical / "start.sh"),
        )
        observation = RuntimeObservation(
            desired_running=False,
            tunnel_fingerprint=self.expected,
            launchd=canonical_job,
            listeners=(),
            processes=(),
        )
        plan = self.recovery().plan(observation)
        self.assertEqual(plan.kind, "noop")
        self.assertEqual(plan.actions, ())

    def test_canonical_plist_includes_launchd_home_and_homebrew_path(self) -> None:
        import plistlib
        payload = plistlib.loads(self.recovery()._canonical_plist_payload())
        env = payload["EnvironmentVariables"]
        self.assertEqual(env["HOME"], str(self.home))
        self.assertEqual(
            env["PATH"],
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        )

    def test_outdated_canonical_stopped_service_plans_bounded_refresh(self) -> None:
        observation = RuntimeObservation(
            False, self.expected, self.canonical_job(), (), (), False
        )
        plan = self.recovery().plan(observation)
        self.assertEqual(plan.kind, "refresh_canonical_service")
        self.assertEqual(
            plan.actions,
            ("write_canonical_plist", "remove_canonical_service_registration", "register_canonical_service"),
        )

    def test_installer_never_bypasses_resolved_launchctl(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        self.assertIn('LAUNCHCTL="$(command -v launchctl)"', text)
        self.assertNotIn("/bin/launchctl", text)
        self.assertIn('"$LAUNCHCTL" print', text)
        self.assertIn('"$LAUNCHCTL" bootstrap', text)

    def test_recover_executes_only_bounded_migration_sequence(self) -> None:
        tunnel, child = self.canonical_processes()
        cutover = RuntimeObservation(
            False, self.expected, self.canonical_job(), (100,), (tunnel, child)
        )
        final = RuntimeObservation(
            False, self.expected, self.canonical_job(), (), ()
        )

        class ScriptedRecovery(RuntimeServiceRecovery):
            def __init__(inner, outer):
                super().__init__(outer.repo, outer.canonical, outer.home, outer.expected)
                inner.observations = [outer.stale_observation(), outer.stale_observation(), cutover, final]
                inner.mutations = []

            def observe(inner):
                return inner.observations.pop(0)

            def _write_canonical_plist(inner):
                inner.mutations.append("write")

            def _remove_stale_registration(inner):
                inner.mutations.append("bootout")

            def _register_canonical_service(inner):
                inner.mutations.append("bootstrap")

            def _terminate_process_group(inner, pgid):
                inner.mutations.append(("terminate", pgid))

        recovery = ScriptedRecovery(self)
        result = recovery.recover()
        self.assertEqual(result.kind, "migrate_stale_fixture")
        self.assertEqual(
            recovery.mutations,
            ["write", "bootout", "bootstrap", ("terminate", 100)],
        )
        self.assertFalse((self.home / "Library/Application Support/Agent Runtime/protected-runtime-running").exists())
        self.assertTrue((self.canonical / ".venv").is_symlink())

    def test_recover_revalidates_stale_fixture_before_bootout(self) -> None:
        changed = RuntimeObservation(
            False,
            self.expected,
            self.canonical_job(),
            (),
            (),
        )

        class RacingRecovery(RuntimeServiceRecovery):
            def __init__(inner, outer):
                super().__init__(outer.repo, outer.canonical, outer.home, outer.expected)
                inner.observations = [outer.stale_observation(), changed]
                inner.mutations = []
            def observe(inner):
                return inner.observations.pop(0)
            def _write_canonical_plist(inner):
                inner.mutations.append("write")
            def _remove_stale_registration(inner):
                inner.mutations.append("bootout")

        recovery = RacingRecovery(self)
        with self.assertRaises(RecoveryError):
            recovery.recover()
        self.assertEqual(recovery.mutations, ["write"])

    def test_recover_is_noop_after_convergence(self) -> None:
        recovery = self.recovery()
        recovery.observe = lambda: RuntimeObservation(
            False, self.expected, self.canonical_job(), (), ()
        )
        result = recovery.recover()
        self.assertEqual(result.kind, "noop")

    def test_observe_parses_exact_service_listener_and_process_tree(self) -> None:
        stale = self.stale_job()
        tunnel, child = self.canonical_processes()

        def runner(argv):
            if argv[0] == "launchctl":
                text = f"path = {stale.path}\nstate = {stale.state}\nprogram = {stale.program}\n"
            elif argv[0] == "lsof":
                text = "100\n"
            elif argv[0] == "ps":
                text = (
                    f"{tunnel.pid} {tunnel.ppid} {tunnel.pgid} {tunnel.executable} {tunnel.argv}\n"
                    f"{child.pid} {child.ppid} {child.pgid} {child.executable} {child.argv}\n"
                )
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, text, "")

        observation = RuntimeServiceRecovery(
            self.repo, self.canonical, self.home, self.expected, runner=runner
        ).observe()
        self.assertEqual(observation, self.stale_observation())

    def test_observe_recovers_full_executable_when_macos_comm_is_truncated(self) -> None:
        stale = self.stale_job()
        tunnel, child = self.canonical_processes()

        def runner(argv):
            if argv[0] == "launchctl":
                text = f"path = {stale.path}\nstate = {stale.state}\nprogram = {stale.program}\n"
            elif argv[0] == "lsof":
                text = "100\n"
            elif argv[0] == "ps":
                text = (
                    f"100 10 100 /opt/homebrew/Ce {tunnel.argv}\n"
                    f"101 100 100 /opt/homebrew/Ce {child.argv}\n"
                )
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, text, "")

        recovery = RuntimeServiceRecovery(
            self.repo, self.canonical, self.home, self.expected, runner=runner
        )
        observation = recovery.observe()
        self.assertEqual(recovery.plan(observation).kind, "migrate_stale_fixture")

    def test_observe_rejects_ambiguous_launchd_metadata(self) -> None:
        def runner(argv):
            if argv[0] == "launchctl":
                return subprocess.CompletedProcess(argv, 0, "state = not running\n", "")
            raise AssertionError(argv)

        recovery = RuntimeServiceRecovery(
            self.repo, self.canonical, self.home, self.expected, runner=runner
        )
        with self.assertRaises(RecoveryError):
            recovery.observe()

    def test_launchd_parser_ignores_nested_state_fields(self) -> None:
        snapshot = RuntimeServiceRecovery._parse_launchd(
            "\tpath = /private/tmp/runtime/com.picmao.agent-runtime-runtime.plist\n"
            "\tstate = not running\n"
            "\tprogram = /private/tmp/runtime/start.sh\n"
            "\tresource coalition = {\n"
            "\t\tstate = active\n"
            "\t}\n"
        )
        self.assertEqual(snapshot.state, "not running")


    def test_mcp_child_requires_exact_canonical_venv_command(self) -> None:
        tunnel, _ = self.canonical_processes()
        child = ProcessSnapshot(
            101, 100, 100,
            "/opt/homebrew/Frameworks/Python.framework/Resources/Python.app/Contents/MacOS/Python",
            "/opt/homebrew/Frameworks/Python.framework/Resources/Python.app/Contents/MacOS/Python -m agent_runtime.server",
        )
        with self.assertRaises(RecoveryError):
            self.recovery().plan(self.stale_observation(processes=(tunnel, child)))

    def test_cli_check_is_nonmutating_and_apply_is_explicit(self) -> None:
        import macos.recover_runtime_service as module

        class FakeRecovery:
            def __init__(self):
                self.recover_calls = 0
            def observe(self):
                return self
            def plan(self, _observation):
                return module.MigrationPlan("migrate_stale_fixture", 100, 100, ("bounded",))
            def recover(self):
                self.recover_calls += 1
                return module.MigrationPlan("migrate_stale_fixture", 100, 100, ("bounded",))

        created = []
        def factory(*_args, **_kwargs):
            item = FakeRecovery(); created.append(item); return item
        common = ["--repository-root", str(self.repo), "--canonical-root", str(self.canonical),
                  "--expected-tunnel-fingerprint", self.expected]
        self.assertEqual(module.main(common, recovery_factory=factory), 0)
        self.assertEqual(created[-1].recover_calls, 0)
        self.assertEqual(module.main(common + ["--apply"], recovery_factory=factory), 0)
        self.assertEqual(created[-1].recover_calls, 1)

    def test_install_recovery_mode_branches_before_generic_venv_install(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        marker = "--recover-runtime-service"
        self.assertIn(marker, text)
        self.assertLess(text.index(marker), text.index('if [[ ! -e "$ROOT/.venv" ]]'))
        self.assertIn('macos/recover_runtime_service.py', text)
        self.assertIn('--canonical-root "$ROOT"', text)
        self.assertIn('--expected-tunnel-fingerprint "6aa2b81d6dd8"', text)


if __name__ == "__main__":
    unittest.main()
