#!/usr/bin/env python3
"""Deterministically freeze one exact non-installed Agent Runtime candidate."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MACOS_ROOT = Path(__file__).resolve().parent
if str(MACOS_ROOT) not in sys.path:
    sys.path.insert(0, str(MACOS_ROOT))
import package_provenance as provenance

HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


class FreezeCandidateError(RuntimeError):
    pass


def _require_identity(value: str, pattern: re.Pattern[str], label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise FreezeCandidateError(f"expected {label} identity is invalid")
    return value


def require_source_identity(
    repo: Path,
    expected_revision: str,
    expected_tree: str,
    expected_lock_sha256: str,
) -> dict[str, object]:
    _require_identity(expected_revision, HEX40, "source revision")
    _require_identity(expected_tree, HEX40, "source tree")
    _require_identity(expected_lock_sha256, HEX64, "requirements.lock")
    try:
        revision, tree = provenance.git_identity(repo)
        lock_sha = provenance.lock_sha256(repo / "requirements.lock")
    except provenance.PackageProvenanceError as exc:
        raise FreezeCandidateError(str(exc)) from exc
    if revision != expected_revision:
        raise FreezeCandidateError("source revision does not match expected freeze identity")
    if tree != expected_tree:
        raise FreezeCandidateError("source tree does not match expected freeze identity")
    if lock_sha != expected_lock_sha256:
        raise FreezeCandidateError("requirements.lock does not match expected freeze identity")
    return {
        "source_revision": revision,
        "source_tree": tree,
        "requirements_lock_sha256": lock_sha,
    }


def require_candidate_identity(
    candidate: dict[str, object],
    expected_revision: str,
    expected_tree: str,
    expected_lock_sha256: str,
) -> dict[str, object]:
    expected = {
        "source_revision": _require_identity(expected_revision, HEX40, "source revision"),
        "source_tree": _require_identity(expected_tree, HEX40, "source tree"),
        "requirements_lock_sha256": _require_identity(
            expected_lock_sha256, HEX64, "requirements.lock"
        ),
    }
    for field, value in expected.items():
        if candidate.get(field) != value:
            raise FreezeCandidateError(
                f"candidate identity does not match expected freeze identity: {field}"
            )
    return candidate


def parse_package_result(repo: Path, stdout: str) -> tuple[Path, Path, str]:
    values: dict[str, str] = {}
    required = {"candidate_app", "candidate_handoff", "candidate_sha256"}
    for line in stdout.splitlines():
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in required or key in values or not value:
            raise FreezeCandidateError("candidate package result is malformed")
        values[key] = value
    if set(values) != required:
        raise FreezeCandidateError("candidate package result is incomplete")

    candidate_sha = _require_identity(values["candidate_sha256"], HEX64, "reported candidate SHA-256")
    candidates_root = (repo / "build" / "candidates").resolve(strict=False)
    expected_root = candidates_root / candidate_sha
    app = Path(values["candidate_app"]).resolve(strict=False)
    handoff = Path(values["candidate_handoff"]).resolve(strict=False)
    if app != expected_root / "Agent Runtime.app":
        raise FreezeCandidateError("candidate package reported an unexpected app path")
    if handoff != expected_root / "Agent Runtime.candidate.json":
        raise FreezeCandidateError("candidate package reported an unexpected handoff path")
    return app, handoff, candidate_sha


def freeze_candidate(
    repo: Path,
    expected_revision: str,
    expected_tree: str,
    expected_lock_sha256: str,
) -> dict[str, object]:
    repo = repo.resolve()
    require_source_identity(repo, expected_revision, expected_tree, expected_lock_sha256)
    package = repo / "macos" / "package_app.sh"
    if package.is_symlink() or not package.is_file():
        raise FreezeCandidateError("package_app.sh must be a regular non-symlink file")
    result = subprocess.run(
        [str(package)],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise FreezeCandidateError(f"candidate package construction failed with exit {result.returncode}")
    app, handoff, reported_sha = parse_package_result(repo, result.stdout)
    require_source_identity(repo, expected_revision, expected_tree, expected_lock_sha256)
    try:
        candidate = provenance.validate_candidate(app, handoff)
    except provenance.PackageProvenanceError as exc:
        raise FreezeCandidateError(str(exc)) from exc
    require_candidate_identity(candidate, expected_revision, expected_tree, expected_lock_sha256)
    if candidate.get("candidate_sha256") != reported_sha:
        raise FreezeCandidateError("reported candidate SHA-256 does not match validated candidate")
    require_source_identity(repo, expected_revision, expected_tree, expected_lock_sha256)
    return {
        **candidate,
        "candidate_app": str(app),
        "candidate_handoff": str(handoff),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and freeze one exact non-installed Agent Runtime candidate."
    )
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--requirements-lock-sha256", required=True)
    args = parser.parse_args()
    repo = MACOS_ROOT.parent
    try:
        candidate = freeze_candidate(
            repo,
            args.source_revision,
            args.source_tree,
            args.requirements_lock_sha256,
        )
    except FreezeCandidateError as exc:
        print("FREEZE ERROR: " + str(exc), file=sys.stderr)
        return 2
    print("FREEZE PASS")
    print(f"candidate_app={candidate['candidate_app']}")
    print(f"candidate_handoff={candidate['candidate_handoff']}")
    print(f"candidate_sha256={candidate['candidate_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
