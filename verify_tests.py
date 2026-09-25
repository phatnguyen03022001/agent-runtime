#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, TextIO

from verification_policy import (
    GLOBAL_QUICK_MODULES,
    GLOBAL_WORKER_LIMIT,
    HEARTBEAT_SECONDS,
    L1_DETERMINISTIC_UNIT,
    L2_ISOLATED_INTEGRATION,
    L3_DETERMINISTIC_REGRESSION,
    L4_HOST_LIFECYCLE,
    L5_QUALIFICATION_CHAOS_CUTOVER,
    LANE_WORKER_LIMITS,
    MODULE_POLICIES,
    ModulePolicy,
    matching_path_rules,
)

ROOT = Path(__file__).resolve().parent
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
PROFILE_QUICK = "quick"
PROFILE_CANDIDATE = "candidate"
PROFILE_QUALIFICATION = "qualification"
PROFILES = (PROFILE_QUICK, PROFILE_CANDIDATE, PROFILE_QUALIFICATION)
PLAN_SCHEMA = 1
LOG_LIMIT_BYTES = 16 * 1024

EXECUTION_LANE_ORDER = {
    L5_QUALIFICATION_CHAOS_CUTOVER: 0,
    L4_HOST_LIFECYCLE: 1,
    L2_ISOLATED_INTEGRATION: 2,
    L3_DETERMINISTIC_REGRESSION: 3,
    L1_DETERMINISTIC_UNIT: 4,
}


@dataclass(frozen=True)
class ModuleResult:
    module: str
    tests: int
    duration_s: float
    success: bool
    output: str = ""


@dataclass(frozen=True)
class SelectedModule:
    module: str
    policy: ModulePolicy
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class VerificationPlan:
    requested_profile: str
    effective_profile: str
    base: str
    head: str
    changed_paths: tuple[str, ...]
    subsystem_triggers: tuple[str, ...]
    escalations: tuple[str, ...]
    docs_only: bool
    selected: tuple[SelectedModule, ...]


@dataclass
class RunningWorker:
    selected: SelectedModule
    process: subprocess.Popen[bytes]
    result_path: Path
    log_path: Path
    log_handle: BinaryIO
    started_at: float


@dataclass(frozen=True)
class VerificationOutcome:
    success: bool
    results: tuple[ModuleResult, ...]
    duration_s: float


def discover_modules(root: Path = ROOT) -> list[str]:
    return [
        f"tests.{path.stem}"
        for path in sorted((root / "tests").glob("test_*.py"))
    ]


def validate_classification(
    modules: Iterable[str],
    policies: dict[str, ModulePolicy] = MODULE_POLICIES,
) -> None:
    discovered = tuple(sorted(modules))
    classified = tuple(sorted(policies))
    unknown = sorted(set(discovered) - set(classified))
    stale = sorted(set(classified) - set(discovered))
    if unknown or stale:
        details: list[str] = []
        if unknown:
            details.append("unclassified=" + ",".join(unknown))
        if stale:
            details.append("missing-test-module=" + ",".join(stale))
        raise ValueError("verification classification mismatch: " + " ".join(details))


def _run_git(
    args: list[str],
    *,
    root: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def git_head(root: Path = ROOT) -> str:
    result = _run_git(["rev-parse", "HEAD"], root=root)
    head = result.stdout.strip()
    if result.returncode != 0 or not SHA40_RE.fullmatch(head):
        raise ValueError(f"unable to resolve exact HEAD: {result.stderr.strip()}")
    return head


def git_clean(root: Path = ROOT) -> bool:
    result = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        root=root,
    )
    if result.returncode != 0:
        raise ValueError(f"unable to inspect repository state: {result.stderr.strip()}")
    return result.stdout == ""


def validate_base(base: str, *, root: Path = ROOT) -> None:
    if not SHA40_RE.fullmatch(base):
        raise ValueError("--base must be an exact lowercase 40-hex commit SHA")
    result = _run_git(["cat-file", "-e", f"{base}^{{commit}}"], root=root)
    if result.returncode != 0:
        raise ValueError(f"--base is not a local commit: {base}")


def changed_paths(base: str, head: str, *, root: Path = ROOT) -> tuple[str, ...]:
    validate_base(base, root=root)
    if not SHA40_RE.fullmatch(head):
        raise ValueError(f"invalid HEAD SHA: {head}")
    result = _run_git(
        [
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            f"{base}..{head}",
            "--",
        ],
        root=root,
    )
    if result.returncode != 0:
        raise ValueError(f"unable to resolve exact base-to-HEAD diff: {result.stderr.strip()}")
    return tuple(sorted(line for line in result.stdout.splitlines() if line))


def _test_module_from_path(path: str) -> str | None:
    candidate = Path(path)
    if (
        candidate.parent.as_posix() == "tests"
        and candidate.name.startswith("test_")
        and candidate.suffix == ".py"
    ):
        return f"tests.{candidate.stem}"
    return None


def analyze_changes(
    paths: Iterable[str],
    *,
    policies: dict[str, ModulePolicy] = MODULE_POLICIES,
) -> tuple[set[str], set[str], list[str], bool]:
    triggers: set[str] = set()
    direct_modules: set[str] = set()
    escalations: list[str] = []
    docs_flags: list[bool] = []

    for path in sorted(set(paths)):
        direct_module = _test_module_from_path(path)
        if direct_module is not None:
            policy = policies.get(direct_module)
            if policy is None:
                escalations.append(f"unknown test module changed: {path}")
                docs_flags.append(False)
                continue
            direct_modules.add(direct_module)
            triggers.update(policy.subsystem_tags)
            docs_flags.append(False)
            if direct_module == "tests.test_verify_harness":
                escalations.append("verification harness tests changed")
            elif policy.lane in {
                L4_HOST_LIFECYCLE,
                L5_QUALIFICATION_CHAOS_CUTOVER,
            }:
                escalations.append(
                    f"high-cost proof module changed directly: {direct_module}"
                )
            continue

        rules = matching_path_rules(path)
        if not rules:
            escalations.append(f"unknown changed path: {path}")
            docs_flags.append(False)
            continue

        path_docs_only = all(rule.docs_only for rule in rules)
        docs_flags.append(path_docs_only)
        for rule in rules:
            triggers.update(rule.subsystem_tags)
            if rule.force_qualification:
                escalations.append(rule.reason or f"qualification required by {rule.pattern}")

    docs_only = bool(docs_flags) and all(docs_flags)
    return triggers, direct_modules, sorted(set(escalations)), docs_only


def _selected_with_reason(
    module: str,
    reasons: Iterable[str],
    *,
    policies: dict[str, ModulePolicy],
) -> SelectedModule:
    return SelectedModule(
        module=module,
        policy=policies[module],
        reasons=tuple(sorted(set(reasons))),
    )


def build_plan(
    *,
    requested_profile: str,
    base: str,
    head: str,
    paths: Iterable[str],
    policies: dict[str, ModulePolicy] = MODULE_POLICIES,
    global_quick_modules: tuple[str, ...] = GLOBAL_QUICK_MODULES,
) -> VerificationPlan:
    if requested_profile not in PROFILES:
        raise ValueError(f"unsupported verification profile: {requested_profile}")

    ordered_paths = tuple(sorted(set(paths)))
    triggers, direct_modules, escalations, docs_only = analyze_changes(
        ordered_paths,
        policies=policies,
    )

    effective_profile = requested_profile
    if requested_profile in {PROFILE_QUICK, PROFILE_CANDIDATE} and escalations:
        effective_profile = PROFILE_QUALIFICATION

    reasons_by_module: dict[str, list[str]] = {}

    def select(module: str, reason: str) -> None:
        reasons_by_module.setdefault(module, []).append(reason)

    if effective_profile == PROFILE_QUALIFICATION:
        escalation_reason = (
            "profile:qualification complete proof surface"
            if not escalations
            else "qualification escalation: " + "; ".join(escalations)
        )
        for module in sorted(policies):
            select(module, escalation_reason)
    elif docs_only:
        pass
    elif effective_profile == PROFILE_CANDIDATE:
        for module, policy in sorted(policies.items()):
            if policy.lane in {
                L1_DETERMINISTIC_UNIT,
                L2_ISOLATED_INTEGRATION,
                L3_DETERMINISTIC_REGRESSION,
            }:
                select(module, "profile:candidate deterministic regression")
            elif set(policy.subsystem_tags).intersection(triggers):
                select(
                    module,
                    "trigger:" + ",".join(
                        sorted(set(policy.subsystem_tags).intersection(triggers))
                    ),
                )
        for module in sorted(direct_modules):
            select(module, "direct-test-change")
    else:
        for module in global_quick_modules:
            if module not in policies:
                raise ValueError(f"unknown globally-required quick module: {module}")
            select(module, "profile:quick global contract")
        for module, policy in sorted(policies.items()):
            if (
                policy.lane
                in {L1_DETERMINISTIC_UNIT, L2_ISOLATED_INTEGRATION}
                and set(policy.subsystem_tags).intersection(triggers)
            ):
                select(
                    module,
                    "trigger:" + ",".join(
                        sorted(set(policy.subsystem_tags).intersection(triggers))
                    ),
                )
        for module in sorted(direct_modules):
            select(module, "direct-test-change")

    selected = tuple(
        _selected_with_reason(module, reasons_by_module[module], policies=policies)
        for module in sorted(reasons_by_module)
    )
    return VerificationPlan(
        requested_profile=requested_profile,
        effective_profile=effective_profile,
        base=base,
        head=head,
        changed_paths=ordered_paths,
        subsystem_triggers=tuple(sorted(triggers)),
        escalations=tuple(escalations),
        docs_only=docs_only,
        selected=selected,
    )


def plan_payload(plan: VerificationPlan) -> dict[str, object]:
    return {
        "schema": PLAN_SCHEMA,
        "requested_profile": plan.requested_profile,
        "effective_profile": plan.effective_profile,
        "base": plan.base,
        "head": plan.head,
        "changed_paths": list(plan.changed_paths),
        "subsystem_triggers": list(plan.subsystem_triggers),
        "escalations": list(plan.escalations),
        "docs_only": plan.docs_only,
        "selected_modules": [
            {
                "module": item.module,
                "lane": item.policy.lane,
                "subsystem_tags": list(item.policy.subsystem_tags),
                "isolation_key": item.policy.isolation_key,
                "proof_rationale": item.policy.proof_rationale,
                "timeout_seconds": item.policy.timeout_seconds,
                "reasons": list(item.reasons),
            }
            for item in plan.selected
        ],
    }


def serialize_plan(plan: VerificationPlan) -> str:
    return json.dumps(plan_payload(plan), indent=2, sort_keys=True) + "\n"


def run_module(module: str) -> ModuleResult:
    stream = io.StringIO()
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    tests = suite.countTestCases()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return ModuleResult(
        module=module,
        tests=tests,
        duration_s=time.monotonic() - started,
        success=result.wasSuccessful(),
        output="" if result.wasSuccessful() else stream.getvalue(),
    )


def write_worker_result(path: Path, result: ModuleResult) -> None:
    payload = {
        "module": result.module,
        "tests": result.tests,
        "duration_s": result.duration_s,
        "success": result.success,
        "output": result.output,
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def load_worker_result(path: Path) -> ModuleResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ModuleResult(
        module=str(payload["module"]),
        tests=int(payload["tests"]),
        duration_s=float(payload["duration_s"]),
        success=bool(payload["success"]),
        output=str(payload.get("output", "")),
    )


def worker_main(result_path: Path, module: str) -> int:
    result = run_module(module)
    write_worker_result(result_path, result)
    return 0 if result.success else 1


def _emit(stream: TextIO, message: str) -> None:
    print(message, file=stream, flush=True)


def _read_bounded_log(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > LOG_LIMIT_BYTES:
        data = data[-LOG_LIMIT_BYTES:]
        prefix = b"...[worker log truncated]...\n"
        data = prefix + data
    return data.decode("utf-8", errors="replace").strip()


def _terminate_owned_group(worker: RunningWorker) -> None:
    process = worker.process
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.75)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _worker_env(
    *,
    root: Path,
    extra_env: dict[str, str] | None,
) -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root) + (os.pathsep + current if current else "")
    if extra_env:
        env.update(extra_env)
    return env


def _launch_worker(
    selected: SelectedModule,
    *,
    root: Path,
    worker_script: Path,
    scratch: Path,
    python_executable: str,
    extra_env: dict[str, str] | None,
    out: TextIO,
) -> RunningWorker:
    safe_name = selected.module.replace(".", "-")
    result_path = scratch / f"{safe_name}.json"
    log_path = scratch / f"{safe_name}.log"
    log_handle = log_path.open("wb")
    isolation = selected.policy.isolation_key or "none"
    _emit(
        out,
        "VERIFY START "
        f"lane={selected.policy.lane} module={selected.module} "
        f"timeout_s={selected.policy.timeout_seconds:g} isolation={isolation}",
    )
    try:
        process = subprocess.Popen(
            [
                python_executable,
                str(worker_script),
                "--worker",
                "--worker-result",
                str(result_path),
                "--worker-module",
                selected.module,
            ],
            cwd=root,
            env=_worker_env(root=root, extra_env=extra_env),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except BaseException:
        log_handle.close()
        raise
    return RunningWorker(
        selected=selected,
        process=process,
        result_path=result_path,
        log_path=log_path,
        log_handle=log_handle,
        started_at=time.monotonic(),
    )


def _execution_key(item: SelectedModule) -> tuple[int, str]:
    return (EXECUTION_LANE_ORDER[item.policy.lane], item.module)


def execute_selected(
    selected_modules: Iterable[SelectedModule],
    *,
    profile: str,
    root: Path = ROOT,
    worker_script: Path | None = None,
    python_executable: str = sys.executable,
    extra_env: dict[str, str] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    global_worker_limit: int = GLOBAL_WORKER_LIMIT,
    lane_worker_limits: dict[str, int] = LANE_WORKER_LIMITS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> VerificationOutcome:
    if global_worker_limit < 1:
        raise ValueError("global worker limit must be positive")
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat interval must be positive")

    all_selected = tuple(selected_modules)
    pending = sorted(all_selected, key=_execution_key)
    if not pending:
        return VerificationOutcome(success=True, results=(), duration_s=0.0)

    script = worker_script or Path(__file__).resolve()
    started_all = time.monotonic()
    results: list[ModuleResult] = []
    active: dict[str, RunningWorker] = {}
    active_lanes: Counter[str] = Counter()
    active_isolation: set[str] = set()
    ok = True
    stop_launching = False
    next_heartbeat = started_all + heartbeat_seconds

    with tempfile.TemporaryDirectory(prefix="agent-runtime-verify-") as raw:
        scratch = Path(raw)

        while pending or active:
            launched = False
            if not stop_launching:
                while len(active) < global_worker_limit:
                    launch_index: int | None = None
                    for index, item in enumerate(pending):
                        lane_limit = lane_worker_limits.get(item.policy.lane)
                        if lane_limit is None or lane_limit < 1:
                            raise ValueError(
                                f"missing/invalid lane worker limit: {item.policy.lane}"
                            )
                        if active_lanes[item.policy.lane] >= lane_limit:
                            continue
                        key = item.policy.isolation_key
                        if key is not None and key in active_isolation:
                            continue
                        launch_index = index
                        break

                    if launch_index is None:
                        break

                    item = pending.pop(launch_index)
                    try:
                        worker = _launch_worker(
                            item,
                            root=root,
                            worker_script=script,
                            scratch=scratch,
                            python_executable=python_executable,
                            extra_env=extra_env,
                            out=out,
                        )
                    except BaseException as exc:
                        _emit(
                            err,
                            f"VERIFY FAIL lane={item.policy.lane} "
                            f"module={item.module} reason=worker-start-error "
                            f"error={type(exc).__name__}:{exc}",
                        )
                        ok = False
                        stop_launching = True
                        break

                    active[item.module] = worker
                    active_lanes[item.policy.lane] += 1
                    if item.policy.isolation_key is not None:
                        active_isolation.add(item.policy.isolation_key)
                    launched = True

            now = time.monotonic()
            completed_any = False
            for module in list(active):
                worker = active[module]
                elapsed = now - worker.started_at
                return_code = worker.process.poll()

                if return_code is None and elapsed <= worker.selected.policy.timeout_seconds:
                    continue

                active.pop(module)
                active_lanes[worker.selected.policy.lane] -= 1
                key = worker.selected.policy.isolation_key
                if key is not None:
                    active_isolation.discard(key)

                if return_code is None:
                    _terminate_owned_group(worker)
                    worker.log_handle.close()
                    _emit(
                        err,
                        "VERIFY TIMEOUT "
                        f"lane={worker.selected.policy.lane} module={module} "
                        f"elapsed_s={elapsed:.3f} "
                        f"timeout_s={worker.selected.policy.timeout_seconds:g}",
                    )
                    worker_log = _read_bounded_log(worker.log_path)
                    if worker_log:
                        _emit(err, worker_log)
                    ok = False
                    stop_launching = True
                    completed_any = True
                    continue

                worker.log_handle.close()
                if not worker.result_path.exists():
                    _emit(
                        err,
                        "VERIFY FAIL "
                        f"lane={worker.selected.policy.lane} module={module} "
                        f"reason=worker-missing-result exit_code={return_code} "
                        f"elapsed_s={elapsed:.3f}",
                    )
                    worker_log = _read_bounded_log(worker.log_path)
                    if worker_log:
                        _emit(err, worker_log)
                    ok = False
                    stop_launching = True
                    completed_any = True
                    continue

                try:
                    result = load_worker_result(worker.result_path)
                except BaseException as exc:
                    _emit(
                        err,
                        "VERIFY FAIL "
                        f"lane={worker.selected.policy.lane} module={module} "
                        f"reason=invalid-worker-result "
                        f"error={type(exc).__name__}:{exc}",
                    )
                    ok = False
                    stop_launching = True
                    completed_any = True
                    continue

                results.append(result)
                if return_code == 0 and result.success:
                    _emit(
                        out,
                        "VERIFY PASS "
                        f"lane={worker.selected.policy.lane} module={module} "
                        f"tests={result.tests} duration_s={result.duration_s:.3f}",
                    )
                else:
                    _emit(
                        err,
                        "VERIFY FAIL "
                        f"lane={worker.selected.policy.lane} module={module} "
                        f"tests={result.tests} duration_s={result.duration_s:.3f} "
                        f"exit_code={return_code}",
                    )
                    if result.output:
                        _emit(err, result.output.rstrip())
                    worker_log = _read_bounded_log(worker.log_path)
                    if worker_log:
                        _emit(err, worker_log)
                    ok = False
                    stop_launching = True
                completed_any = True

            now = time.monotonic()
            if completed_any:
                next_heartbeat = now + heartbeat_seconds
            elif active and now >= next_heartbeat:
                descriptions = ",".join(
                    f"{worker.selected.policy.lane}:{module}:"
                    f"{now - worker.started_at:.1f}s"
                    for module, worker in sorted(active.items())
                )
                _emit(
                    out,
                    "VERIFY HEARTBEAT "
                    f"profile={profile} elapsed_s={now - started_all:.1f} "
                    f"active={descriptions}",
                )
                next_heartbeat = now + heartbeat_seconds

            if stop_launching and not active:
                break
            if active:
                time.sleep(0.05)

    return VerificationOutcome(
        success=ok and not pending and len(results) == len(all_selected),
        results=tuple(sorted(results, key=lambda item: item.module)),
        duration_s=time.monotonic() - started_all,
    )


def print_summary(
    *,
    plan: VerificationPlan,
    outcome: VerificationOutcome,
    total_classified_modules: int,
    out: TextIO = sys.stdout,
) -> None:
    policy_by_module = {item.module: item.policy for item in plan.selected}
    for lane in (
        L1_DETERMINISTIC_UNIT,
        L2_ISOLATED_INTEGRATION,
        L3_DETERMINISTIC_REGRESSION,
        L4_HOST_LIFECYCLE,
        L5_QUALIFICATION_CHAOS_CUTOVER,
    ):
        lane_results = [
            item
            for item in outcome.results
            if policy_by_module.get(item.module)
            and policy_by_module[item.module].lane == lane
        ]
        selected_count = sum(
            1 for item in plan.selected if item.policy.lane == lane
        )
        if selected_count:
            _emit(
                out,
                f"VERIFY lane={lane} selected_modules={selected_count} "
                f"completed_modules={len(lane_results)} "
                f"tests={sum(item.tests for item in lane_results)} "
                f"module_work_s={sum(item.duration_s for item in lane_results):.3f}",
            )

    slowest = sorted(
        outcome.results,
        key=lambda item: (-item.duration_s, item.module),
    )[:5]
    _emit(
        out,
        "VERIFY slowest="
        + ",".join(
            f"{item.module}:{item.duration_s:.3f}s"
            for item in slowest
        ),
    )
    _emit(
        out,
        f"VERIFY total modules={len(outcome.results)}/{len(plan.selected)} "
        f"classified_modules={total_classified_modules} "
        f"tests={sum(item.tests for item in outcome.results)} "
        f"duration_s={outcome.duration_s:.3f} "
        f"profile={plan.effective_profile} "
        f"requested_profile={plan.requested_profile}",
    )


def resolve_plan(
    *,
    requested_profile: str,
    base: str | None,
    root: Path = ROOT,
) -> VerificationPlan:
    modules = discover_modules(root)
    validate_classification(modules)
    head = git_head(root)

    if requested_profile in {PROFILE_QUICK, PROFILE_CANDIDATE} and base is None:
        raise ValueError(f"--profile {requested_profile} requires --base <40-hex>")

    resolved_base = base or head
    paths = changed_paths(resolved_base, head, root=root)
    return build_plan(
        requested_profile=requested_profile,
        base=resolved_base,
        head=head,
        paths=paths,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent Runtime deterministic verification runner"
    )
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--base")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Compatibility alias for --profile quick; still requires --base.",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-result", help=argparse.SUPPRESS)
    parser.add_argument("--worker-module", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.worker:
        if not args.worker_result or not args.worker_module:
            raise SystemExit("--worker requires --worker-result and --worker-module")
        return worker_main(Path(args.worker_result), args.worker_module)

    if args.quick and args.profile not in {None, PROFILE_QUICK}:
        raise SystemExit("--quick conflicts with a non-quick --profile")
    if args.plan and args.profile is None and not args.quick:
        raise SystemExit("--plan requires --profile")
    requested_profile = (
        PROFILE_QUICK
        if args.quick
        else (args.profile or PROFILE_QUALIFICATION)
    )
    explicit_profile = args.profile is not None or args.quick

    try:
        if (explicit_profile or args.plan) and not git_clean(ROOT):
            raise ValueError(
                "explicit profile/plan requires an exact clean candidate HEAD"
            )
        plan = resolve_plan(
            requested_profile=requested_profile,
            base=args.base,
            root=ROOT,
        )
    except ValueError as exc:
        print(f"VERIFY policy error: {exc}", file=sys.stderr)
        return 2

    if args.plan:
        sys.stdout.write(serialize_plan(plan))
        sys.stdout.flush()
        return 0

    outcome = execute_selected(
        plan.selected,
        profile=plan.effective_profile,
    )
    print_summary(
        plan=plan,
        outcome=outcome,
        total_classified_modules=len(MODULE_POLICIES),
    )
    return 0 if outcome.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
