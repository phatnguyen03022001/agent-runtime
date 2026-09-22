#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKER_COUNT = 4
SERIAL_RELOAD_ISOLATION_BOUNDARY = "tests.test_task0041_boundedness"

# Explicit allowlist backed by pure/read-only tests or disposable, per-test
# fixtures. Lifecycle, service, cutover, terminal, tunnel, and unknown modules
# stay serial even when they currently appear deterministic.
#
# Weights are the green Phase A sequential measurements, used only for stable
# x4 shard balancing; membership is the parallel-safety decision.
FAST_PARALLEL_SAFE_WEIGHTS = {
    "tests.test_capability_registry": 0.769,
    "tests.test_fs_list": 0.105,
    "tests.test_fs_patch": 0.117,
    "tests.test_fs_read_batch": 0.543,
    "tests.test_fs_read_batch_adversarial": 0.502,
    "tests.test_fs_search": 0.182,
    "tests.test_fs_write": 0.127,
    "tests.test_fs_write_mcp": 0.422,
    "tests.test_package_provenance": 0.294,
    "tests.test_repo_commit": 12.527,
    "tests.test_repo_diff": 1.143,
    "tests.test_repo_fast_forward": 5.848,
    "tests.test_repo_publish": 11.118,
    "tests.test_repo_publish_mcp": 0.547,
    "tests.test_repo_stage": 8.546,
    "tests.test_repo_stage_commit_mcp": 3.630,
    "tests.test_runtime_config": 0.072,
    "tests.test_runtime_label_split": 0.042,
    "tests.test_schema_export": 1.092,
    "tests.test_task0140_qualification": 0.604,
    "tests.test_timing": 0.418,
    "tests.test_tool_contract": 0.410,
}


@dataclass(frozen=True)
class ModuleResult:
    module: str
    tests: int
    duration_s: float
    success: bool
    output: str = ""


def discover_modules() -> list[str]:
    return [f"tests.{path.stem}" for path in sorted((ROOT / "tests").glob("test_*.py"))]


def classify_modules(modules: list[str]) -> tuple[list[str], list[str]]:
    fast = [module for module in modules if module in FAST_PARALLEL_SAFE_WEIGHTS]
    serial = [module for module in modules if module not in FAST_PARALLEL_SAFE_WEIGHTS]
    return fast, serial


def build_fast_shards(modules: list[str]) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(WORKER_COUNT)]
    loads = [0.0] * WORKER_COUNT
    ordered = sorted(
        modules,
        key=lambda module: (-FAST_PARALLEL_SAFE_WEIGHTS[module], module),
    )
    for module in ordered:
        index = min(range(WORKER_COUNT), key=lambda item: (loads[item], item))
        shards[index].append(module)
        loads[index] += FAST_PARALLEL_SAFE_WEIGHTS[module]
    return shards


def serial_worker_groups(modules: list[str]) -> list[list[str]]:
    """Keep the known MCP-module reload test isolated without parallelizing serial work."""

    if SERIAL_RELOAD_ISOLATION_BOUNDARY not in modules:
        return [modules]
    boundary = modules.index(SERIAL_RELOAD_ISOLATION_BOUNDARY)
    return [modules[:boundary], modules[boundary:]]


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


def run_modules(modules: list[str]) -> list[ModuleResult]:
    results: list[ModuleResult] = []
    for module in modules:
        result = run_module(module)
        results.append(result)
        if not result.success:
            break
    return results


def write_worker_result(path: Path, results: list[ModuleResult]) -> None:
    payload = {
        "results": [
            {
                "module": item.module,
                "tests": item.tests,
                "duration_s": item.duration_s,
                "success": item.success,
                "output": item.output,
            }
            for item in results
        ]
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def worker_main(result_path: Path, modules: list[str]) -> int:
    results = run_modules(modules)
    write_worker_result(result_path, results)
    return 0 if all(item.success for item in results) else 1


def load_worker_result(path: Path) -> list[ModuleResult]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        ModuleResult(
            module=item["module"],
            tests=int(item["tests"]),
            duration_s=float(item["duration_s"]),
            success=bool(item["success"]),
            output=str(item.get("output", "")),
        )
        for item in payload["results"]
    ]


def print_failure(result: ModuleResult) -> None:
    print(f"VERIFY failure module={result.module}", file=sys.stderr)
    if result.output:
        print(result.output.rstrip(), file=sys.stderr)


def print_summary(
    *,
    modules: list[str],
    fast_modules: list[str],
    serial_modules: list[str],
    fast_results: list[ModuleResult],
    serial_results: list[ModuleResult],
    fast_duration_s: float,
    serial_duration_s: float,
    total_duration_s: float,
    quick: bool,
) -> None:
    combined = sorted(fast_results + serial_results, key=lambda item: item.module)
    lane_by_module = {module: "FAST_PARALLEL_SAFE" for module in fast_modules}
    lane_by_module.update({module: "SERIAL_HOST_SENSITIVE" for module in serial_modules})
    for item in combined:
        print(
            "VERIFY module="
            f"{item.module} lane={lane_by_module[item.module]} "
            f"tests={item.tests} duration_s={item.duration_s:.3f}"
        )

    print(
        f"VERIFY lane=FAST_PARALLEL_SAFE workers={WORKER_COUNT} "
        f"tests={sum(item.tests for item in fast_results)} duration_s={fast_duration_s:.3f}"
    )
    if not quick:
        print(
            "VERIFY lane=SERIAL_HOST_SENSITIVE workers=1 "
            f"tests={sum(item.tests for item in serial_results)} duration_s={serial_duration_s:.3f}"
        )

    slowest = sorted(combined, key=lambda item: (-item.duration_s, item.module))[:5]
    print(
        "VERIFY slowest="
        + ",".join(f"{item.module}:{item.duration_s:.3f}s" for item in slowest)
    )
    print(
        f"VERIFY total modules={len(combined)}/{len(modules)} "
        f"tests={sum(item.tests for item in combined)} duration_s={total_duration_s:.3f} "
        f"mode={'quick' if quick else 'full'}"
    )


def run_parallel_lane(modules: list[str]) -> tuple[list[ModuleResult], float, bool]:
    shards = build_fast_shards(modules)
    started = time.monotonic()
    results: list[ModuleResult] = []
    ok = True

    with tempfile.TemporaryDirectory(prefix="agent-runtime-verify-") as raw:
        root = Path(raw)
        processes: list[tuple[subprocess.Popen[bytes], Path, Path, object]] = []
        for index, shard in enumerate(shards):
            result_path = root / f"worker-{index}.json"
            log_path = root / f"worker-{index}.log"
            log_handle = log_path.open("wb")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(result_path),
                    *shard,
                ],
                cwd=ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            processes.append((process, result_path, log_path, log_handle))

        for process, result_path, log_path, log_handle in processes:
            return_code = process.wait()
            log_handle.close()
            if result_path.exists():
                worker_results = load_worker_result(result_path)
                results.extend(worker_results)
                failed = [item for item in worker_results if not item.success]
                for item in failed:
                    print_failure(item)
                if failed:
                    ok = False
            else:
                ok = False
                print("VERIFY worker exited without a result", file=sys.stderr)
            if return_code != 0:
                ok = False
                worker_log = log_path.read_text(encoding="utf-8", errors="replace").strip()
                if worker_log:
                    print(worker_log, file=sys.stderr)

    return results, time.monotonic() - started, ok


def run_serial_lane(modules: list[str]) -> tuple[list[ModuleResult], float, bool]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agent-runtime-verify-serial-") as raw:
        root = Path(raw)
        results: list[ModuleResult] = []
        for index, group in enumerate(serial_worker_groups(modules)):
            result_path = root / f"serial-{index}.json"
            log_path = root / f"serial-{index}.log"
            with log_path.open("wb") as log_handle:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(result_path),
                        *group,
                    ],
                    cwd=ROOT,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                return_code = process.wait()

            if not result_path.exists():
                worker_log = log_path.read_text(encoding="utf-8", errors="replace").strip()
                print("VERIFY serial worker exited without a result", file=sys.stderr)
                if worker_log:
                    print(worker_log, file=sys.stderr)
                return results, time.monotonic() - started, False

            worker_results = load_worker_result(result_path)
            results.extend(worker_results)
            failed = [item for item in worker_results if not item.success]
            for item in failed:
                print_failure(item)
            if return_code != 0 and not failed:
                worker_log = log_path.read_text(encoding="utf-8", errors="replace").strip()
                if worker_log:
                    print(worker_log, file=sys.stderr)
            if return_code != 0 or failed:
                return results, time.monotonic() - started, False

        return results, time.monotonic() - started, True


def verification_main(*, quick: bool) -> int:
    modules = discover_modules()
    fast_modules, serial_modules = classify_modules(modules)
    started = time.monotonic()

    serial_results: list[ModuleResult] = []
    serial_duration_s = 0.0
    if not quick:
        serial_results, serial_duration_s, serial_ok = run_serial_lane(serial_modules)
        if not serial_ok:
            return 1

    fast_results, fast_duration_s, fast_ok = run_parallel_lane(fast_modules)
    if not fast_ok:
        return 1

    total_duration_s = time.monotonic() - started
    print_summary(
        modules=modules,
        fast_modules=fast_modules,
        serial_modules=serial_modules,
        fast_results=fast_results,
        serial_results=serial_results,
        fast_duration_s=fast_duration_s,
        serial_duration_s=serial_duration_s,
        total_duration_s=total_duration_s,
        quick=quick,
    )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent Runtime verification test runner")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("worker_args", nargs="*")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.worker:
        if not args.worker_args:
            raise SystemExit("--worker requires a result path")
        return worker_main(Path(args.worker_args[0]), args.worker_args[1:])
    if args.worker_args:
        raise SystemExit(f"unexpected arguments: {' '.join(args.worker_args)}")
    return verification_main(quick=args.quick)


if __name__ == "__main__":
    raise SystemExit(main())
