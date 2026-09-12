from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

LABEL = "com.picmao.agent-runtime-runtime"
ENV_RELATIVE = Path(".env")
LEGACY_CONFIG_RELATIVE = Path(".config/tunnel-client/agent-runtime.yaml")
DESIRED_RELATIVE = Path("Library/Application Support/Agent Runtime/protected-runtime-running")
RUNTIME_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaunchdSnapshot:
    path: str
    state: str
    program: str


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    ppid: int
    pgid: int
    executable: str
    argv: str


@dataclass(frozen=True)
class RuntimeObservation:
    desired_running: bool
    tunnel_fingerprint: str
    launchd: LaunchdSnapshot | None
    listeners: tuple[int, ...]
    processes: tuple[ProcessSnapshot, ...]
    launchd_environment_ready: bool = True


@dataclass(frozen=True)
class MigrationPlan:
    kind: str
    tunnel_pid: int | None = None
    tunnel_pgid: int | None = None
    actions: tuple[str, ...] = ()


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def tunnel_fingerprint(tunnel_id: str) -> str:
    return hashlib.sha256(tunnel_id.encode()).hexdigest()[:12]


class RuntimeServiceRecovery:
    def __init__(
        self,
        repository_root: Path,
        canonical_root: Path,
        home: Path,
        expected_tunnel_fingerprint: str,
        runner: Runner = _default_runner,
        launchctl: str = "launchctl",
        signaler: Callable[[int, int], None] = os.killpg,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.canonical_root = canonical_root.resolve()
        self.home = home.resolve()
        self.expected_tunnel_fingerprint = expected_tunnel_fingerprint
        self.runner = runner
        self.launchctl = launchctl
        self.signaler = signaler
        self.sleeper = sleeper
        self.service = f"gui/{os.getuid()}/{LABEL}"

    @staticmethod
    def is_proven_stale_fixture(snapshot: LaunchdSnapshot) -> bool:
        if snapshot.state.strip().lower() != "not running":
            return False
        path = Path(snapshot.path)
        program = Path(snapshot.program)
        if path.name != f"{LABEL}.plist" or program.name != "start.sh":
            return False
        path_parts = path.parts
        program_parts = program.parts
        if len(path_parts) < 4 or len(program_parts) < 4:
            return False
        if path_parts[:3] != ("/", "private", "tmp"):
            return False
        return program_parts[:4] == path_parts[:4]
    def _canonical_job(self, snapshot: LaunchdSnapshot) -> bool:
        expected_path = self.home / "Library/LaunchAgents" / f"{LABEL}.plist"
        expected_program = self.canonical_root / "start.sh"
        return (
            snapshot.state.strip().lower() == "not running"
            and Path(snapshot.path) == expected_path
            and Path(snapshot.program) == expected_program
        )

    def _canonical_tunnel(self, process: ProcessSnapshot) -> bool:
        if Path(process.executable).name != "tunnel-client":
            return False
        try:
            argv = shlex.split(process.argv)
        except ValueError:
            return False
        command = f"command={self.canonical_root}/.venv/bin/python -m agent_runtime.server,channel=main"
        common = ["run", "--control-plane.poll-channel", "main", "--mcp.command"]
        suffix = ["--health.listen-addr", "127.0.0.1:8080"]
        return Path(argv[0]).name == "tunnel-client" and tuple(argv[1:]) in {
            tuple(common + [command] + suffix),
            tuple(common + [f"command={self.canonical_root}/.venv/bin/python", "-m", "agent_runtime.server,channel=main"] + suffix),
        }
    def _mcp_child(self, process: ProcessSnapshot, tunnel: ProcessSnapshot) -> bool:
        try:
            argv = shlex.split(process.argv)
        except ValueError:
            return False
        return (
            process.ppid == tunnel.pid
            and process.pgid == tunnel.pgid
            and len(argv) == 3
            and argv[1:] == ["-m", "agent_runtime.server"]
            and Path(argv[0]) == self.canonical_root / ".venv/bin/python"
        )

    @staticmethod
    def _looks_like_mcp(process: ProcessSnapshot) -> bool:
        try:
            argv = shlex.split(process.argv)
        except ValueError:
            return False
        return len(argv) >= 3 and argv[-2:] == ["-m", "agent_runtime.server"]

    def plan(self, observation: RuntimeObservation) -> MigrationPlan:
        if observation.desired_running:
            raise RecoveryError("migration requires desired state STOPPED")
        if observation.tunnel_fingerprint != self.expected_tunnel_fingerprint:
            raise RecoveryError("canonical tunnel fingerprint does not match accepted identity")
        if observation.launchd is None:
            raise RecoveryError("canonical Runtime launchd label is not loaded")
        canonical_tunnels = [p for p in observation.processes if self._canonical_tunnel(p)]
        mcp_processes = [p for p in observation.processes if self._looks_like_mcp(p)]

        if self._canonical_job(observation.launchd):
            if observation.listeners or canonical_tunnels or mcp_processes:
                raise RecoveryError("canonical STOPPED service has ambiguous live Runtime state")
            if observation.launchd_environment_ready:
                return MigrationPlan(kind="noop")
            return MigrationPlan(
                kind="refresh_canonical_service",
                actions=(
                    "write_canonical_plist",
                    "remove_canonical_service_registration",
                    "register_canonical_service",
                ),
            )

        if not self.is_proven_stale_fixture(observation.launchd):
            raise RecoveryError("loaded Runtime service provenance is not the proven stale fixture")
        if len(observation.listeners) != 1:
            raise RecoveryError("port 8080 must have exactly one proven listener")
        if len(canonical_tunnels) != 1:
            raise RecoveryError("expected exactly one canonical unsupervised tunnel-client")

        tunnel = canonical_tunnels[0]
        if observation.listeners[0] != tunnel.pid:
            raise RecoveryError("port 8080 listener is not the canonical tunnel-client")
        children = [p for p in mcp_processes if self._mcp_child(p, tunnel)]
        if len(mcp_processes) != 1 or len(children) != 1:
            raise RecoveryError("canonical Runtime process tree is ambiguous")
        group_members = [p for p in observation.processes if p.pgid == tunnel.pgid]
        if {p.pid for p in group_members} != {tunnel.pid, children[0].pid}:
            raise RecoveryError("canonical Runtime process group contains unrelated members")

        return MigrationPlan(
            kind="migrate_stale_fixture",
            tunnel_pid=tunnel.pid,
            tunnel_pgid=tunnel.pgid,
            actions=(
                "write_canonical_plist",
                "remove_stale_fixture_registration",
                "register_canonical_service",
                "terminate_unsupervised_runtime",
            ),
        )

    @property
    def desired_path(self) -> Path:
        return self.home / DESIRED_RELATIVE

    @property
    def env_path(self) -> Path:
        return self.canonical_root / ENV_RELATIVE

    @property
    def legacy_config_path(self) -> Path:
        return self.home / LEGACY_CONFIG_RELATIVE

    @property
    def canonical_plist(self) -> Path:
        return self.home / "Library/LaunchAgents" / f"{LABEL}.plist"

    def _assert_desired_stopped(self) -> None:
        if self.desired_path.exists():
            raise RecoveryError("desired state changed from STOPPED during migration")

    def _assert_static_prerequisites(self) -> bytes:
        self._assert_desired_stopped()
        env_bytes = self._assert_env_identity_ownership()
        candidate_start = self.repository_root / "start.sh"
        canonical_start = self.canonical_root / "start.sh"
        if not candidate_start.is_file() or not canonical_start.is_file():
            raise RecoveryError("candidate and canonical start.sh must both exist")
        if hashlib.sha256(candidate_start.read_bytes()).digest() != hashlib.sha256(canonical_start.read_bytes()).digest():
            raise RecoveryError("canonical start.sh does not match the verified candidate")
        return env_bytes

    def _assert_env_identity_ownership(self) -> bytes:
        if self.legacy_config_path.exists() or self.legacy_config_path.is_symlink():
            raise RecoveryError("legacy tunnel configuration is present")
        env_file = self.env_path
        if not env_file.is_file() or env_file.is_symlink():
            raise RecoveryError("canonical .env must be a regular non-symlink file")
        raw = env_file.read_bytes()
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise RecoveryError("canonical .env is not valid UTF-8") from exc
        required = {
            "CONTROL_PLANE_API_KEY",
            "CONTROL_PLANE_TUNNEL_ID",
            "AGENT_RUNTIME_WORKSPACE_ROOT",
        }
        values: dict[str, str] = {}
        for number, line in enumerate(lines, start=1):
            if not line or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise RecoveryError(f"canonical .env has malformed entry at line {number}")
            key, value = line.split("=", 1)
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) is None:
                raise RecoveryError(f"canonical .env has malformed key at line {number}")
            if key in required:
                if key in values:
                    raise RecoveryError(f"canonical .env has duplicate {key}")
                values[key] = value
        missing = [key for key in required if not values.get(key, "")]
        if missing:
            raise RecoveryError("canonical .env is missing required Runtime configuration")
        if tunnel_fingerprint(values["CONTROL_PLANE_TUNNEL_ID"]) != self.expected_tunnel_fingerprint:
            raise RecoveryError("canonical tunnel fingerprint does not match accepted identity")
        workspace = Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
        if not workspace.is_absolute() or not workspace.is_dir():
            raise RecoveryError("canonical .env workspace root is invalid")
        return raw

    def _canonical_plist_payload(self) -> bytes:
        payload = {
            "Label": LABEL,
            "ProgramArguments": [
                str(self.canonical_root / "start.sh"),
                "--serve",
                "/opt/homebrew/bin/tunnel-client",
            ],
            "EnvironmentVariables": {
                "HOME": str(self.home),
                "PATH": RUNTIME_PATH,
            },
            "RunAtLoad": False,
            "KeepAlive": {"PathState": {str(self.desired_path): True}},
            "ProcessType": "Interactive",
            "ThrottleInterval": 2,
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)

    def _write_canonical_plist(self) -> None:
        self._assert_desired_stopped()
        self.canonical_plist.parent.mkdir(parents=True, exist_ok=True)
        if self.canonical_plist.is_symlink():
            raise RecoveryError("canonical Runtime plist must not be a symlink")
        tmp = self.canonical_plist.with_name(f".{self.canonical_plist.name}.{os.getpid()}.tmp")
        tmp.write_bytes(self._canonical_plist_payload())
        tmp.chmod(0o600)
        os.replace(tmp, self.canonical_plist)
    def _run_checked(self, argv: Sequence[str], description: str) -> subprocess.CompletedProcess[str]:
        result = self.runner(argv)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RecoveryError(f"{description} failed: {detail[:300]}")
        return result

    def _remove_stale_registration(self) -> None:
        self._assert_desired_stopped()
        self._run_checked(
            [self.launchctl, "bootout", self.service],
            "stale fixture launchd removal",
        )

    def _remove_canonical_registration(self) -> None:
        self._assert_desired_stopped()
        self._run_checked(
            [self.launchctl, "bootout", self.service],
            "outdated canonical launchd removal",
        )

    def _register_canonical_service(self) -> None:
        self._assert_desired_stopped()
        domain = self.service.rsplit("/", 1)[0]
        self._run_checked(
            [self.launchctl, "bootstrap", domain, str(self.canonical_plist)],
            "canonical Runtime launchd registration",
        )
    def _terminate_process_group(self, pgid: int) -> None:
        self._assert_desired_stopped()
        self.signaler(pgid, signal.SIGTERM)

    def _validate_cutover_state(self, observation: RuntimeObservation, plan: MigrationPlan) -> None:
        if observation.desired_running:
            raise RecoveryError("desired state changed during migration")
        if observation.tunnel_fingerprint != self.expected_tunnel_fingerprint:
            raise RecoveryError("tunnel fingerprint changed during migration")
        if observation.launchd is None or not self._canonical_job(observation.launchd):
            raise RecoveryError("canonical supervisor is not registered in STOPPED state")
        canonical_tunnels = [p for p in observation.processes if self._canonical_tunnel(p)]
        if len(canonical_tunnels) != 1:
            raise RecoveryError("cutover Runtime ownership is ambiguous")
        tunnel = canonical_tunnels[0]
        if tunnel.pid != plan.tunnel_pid or tunnel.pgid != plan.tunnel_pgid:
            raise RecoveryError("cutover Runtime identity changed before termination")
        if observation.listeners != (tunnel.pid,):
            raise RecoveryError("cutover listener ownership changed before termination")
        children = [p for p in observation.processes if self._mcp_child(p, tunnel)]
        mcp_processes = [p for p in observation.processes if self._looks_like_mcp(p)]
        if len(children) != 1 or len(mcp_processes) != 1:
            raise RecoveryError("cutover Runtime process tree changed before termination")
        group_members = [p for p in observation.processes if p.pgid == tunnel.pgid]
        if {p.pid for p in group_members} != {tunnel.pid, children[0].pid}:
            raise RecoveryError("cutover Runtime process group contains unrelated members")

    def recover(self) -> MigrationPlan:
        initial = self.observe()
        plan = self.plan(initial)
        if plan.kind == "noop":
            return plan

        env_bytes = self._assert_static_prerequisites()
        self._write_canonical_plist()

        if plan.kind == "refresh_canonical_service":
            self._remove_canonical_registration()
            self._register_canonical_service()
            final = self.plan(self.observe())
            if final.kind != "noop":
                raise RecoveryError("canonical Runtime service did not refresh into STOPPED convergence")
            if self._assert_env_identity_ownership() != env_bytes:
                raise RecoveryError("canonical .env changed during service recovery")
            return plan

        revalidated = self.plan(self.observe())
        if (
            revalidated.kind != plan.kind
            or revalidated.tunnel_pid != plan.tunnel_pid
            or revalidated.tunnel_pgid != plan.tunnel_pgid
        ):
            raise RecoveryError("stale-live ownership changed before launchd removal")

        self._remove_stale_registration()
        self._register_canonical_service()

        cutover = self.observe()
        self._validate_cutover_state(cutover, plan)
        if plan.tunnel_pgid is None:
            raise RecoveryError("migration plan has no proven Runtime process group")
        self._terminate_process_group(plan.tunnel_pgid)

        deadline = time.monotonic() + 5.0
        while True:
            final = self.observe()
            try:
                final_plan = self.plan(final)
            except RecoveryError:
                if time.monotonic() >= deadline:
                    raise RecoveryError("canonical Runtime did not converge to STOPPED after cutover")
                self.sleeper(0.05)
                continue
            if final_plan.kind != "noop":
                raise RecoveryError("post-migration state is not converged")
            break

        if self._assert_env_identity_ownership() != env_bytes:
            raise RecoveryError("canonical .env changed during service recovery")
        self._assert_desired_stopped()
        return plan

    @staticmethod
    def _parse_launchd(text: str) -> LaunchdSnapshot:
        values: dict[str, list[str]] = {"path": [], "state": [], "program": []}
        top_level_indented = any(
            raw.startswith(f"\t{key} = ")
            for raw in text.splitlines()
            for key in values
        )
        for raw in text.splitlines():
            if top_level_indented and (not raw.startswith("\t") or raw.startswith("\t\t")):
                continue
            line = raw.strip()
            for key in values:
                prefix = f"{key} = "
                if line.startswith(prefix):
                    values[key].append(line[len(prefix):].strip())
        if any(len(items) != 1 or not items[0] for items in values.values()):
            raise RecoveryError("launchd metadata is incomplete or ambiguous")
        return LaunchdSnapshot(
            path=values["path"][0],
            state=values["state"][0],
            program=values["program"][0],
        )

    @staticmethod
    def _parse_processes(text: str) -> tuple[ProcessSnapshot, ...]:
        processes: list[ProcessSnapshot] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(None, 4)
            if len(parts) != 5:
                raise RecoveryError("process table contains an ambiguous row")
            try:
                pid, ppid, pgid = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError as exc:
                raise RecoveryError("process table contains a non-numeric process identity") from exc
            command = parts[4]
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                raise RecoveryError("process table contains malformed command argv") from exc
            if not argv:
                raise RecoveryError("process table contains an empty command argv")
            processes.append(ProcessSnapshot(pid, ppid, pgid, argv[0], command))
        return tuple(processes)

    def _canonical_plist_environment_is_ready(self) -> bool:
        try:
            payload = plistlib.loads(self.canonical_plist.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            return False
        return (
            payload.get("ProgramArguments") == [
                str(self.canonical_root / "start.sh"),
                "--serve",
                "/opt/homebrew/bin/tunnel-client",
            ]
            and payload.get("EnvironmentVariables") == {
                "HOME": str(self.home),
                "PATH": RUNTIME_PATH,
            }
        )

    def observe(self) -> RuntimeObservation:
        env_bytes = self._assert_env_identity_ownership()
        tunnel_id = next(
            line.split("=", 1)[1]
            for line in env_bytes.decode("utf-8").splitlines()
            if line.startswith("CONTROL_PLANE_TUNNEL_ID=")
        )
        fingerprint = tunnel_fingerprint(tunnel_id)

        launchd_result = self.runner([self.launchctl, "print", self.service])
        launchd = None
        if launchd_result.returncode == 0:
            launchd = self._parse_launchd(launchd_result.stdout)

        lsof_result = self.runner([
            "lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN", "-t"
        ])
        if lsof_result.returncode not in (0, 1):
            raise RecoveryError("could not inspect protected port 8080")
        try:
            listeners = tuple(sorted({int(x) for x in lsof_result.stdout.split()}))
        except ValueError as exc:
            raise RecoveryError("protected-port ownership is ambiguous") from exc

        ps_result = self.runner([
            "ps", "-axo", "pid=,ppid=,pgid=,comm=,command="
        ])
        if ps_result.returncode != 0:
            raise RecoveryError("could not inspect Runtime process tree")
        return RuntimeObservation(
            desired_running=self.desired_path.exists(),
            tunnel_fingerprint=fingerprint,
            launchd=launchd,
            listeners=listeners,
            processes=self._parse_processes(ps_result.stdout),
            launchd_environment_ready=self._canonical_plist_environment_is_ready(),
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    recovery_factory=RuntimeServiceRecovery,
) -> int:
    parser = argparse.ArgumentParser(description="Bounded Agent Runtime stale-service recovery")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--canonical-root", required=True)
    parser.add_argument("--expected-tunnel-fingerprint", required=True)
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--launchctl", default="launchctl")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        recovery = recovery_factory(
            Path(args.repository_root),
            Path(args.canonical_root),
            Path(args.home),
            args.expected_tunnel_fingerprint,
            launchctl=args.launchctl,
        )
        plan = recovery.recover() if args.apply else recovery.plan(recovery.observe())
    except RecoveryError as exc:
        print(f"RECOVERY_BLOCKED: {exc}", file=sys.stderr)
        return 2
    actions = ",".join(plan.actions) if plan.actions else "none"
    print(f"RECOVERY_{'APPLIED' if args.apply else 'CHECK'}: {plan.kind}; actions={actions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
