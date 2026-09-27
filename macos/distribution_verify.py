#!/usr/bin/env python3
from __future__ import annotations

"""Verify a post-staple Agent Runtime distribution candidate before sealing."""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import package_provenance as provenance

DEVELOPER_ID_PREFIX = "Developer ID Application: "
RESPONSIBLE_EXECUTABLES = (
    "Contents/MacOS/AgentRuntimeMenuBar",
    "Contents/MacOS/AgentRuntimeRuntimeService",
    "Contents/MacOS/AgentRuntimeScreenCapture",
)
RUNTIME_PYTHON = "Contents/Resources/runtime/.venv/bin/python"


class DistributionVerificationError(RuntimeError):
    pass


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DistributionVerificationError("distribution verification command failed") from exc


def _require_ok(argv: list[str], message: str) -> subprocess.CompletedProcess[str]:
    result = _run(argv)
    if result.returncode != 0:
        raise DistributionVerificationError(message)
    return result


def _validated_signing_identity(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(DEVELOPER_ID_PREFIX)
        or value == DEVELOPER_ID_PREFIX
        or any(ch in value for ch in ("\x00", "\r", "\n"))
    ):
        raise DistributionVerificationError(
            "signing identity must be an explicit Developer ID Application identity"
        )
    return value


def _signature_identity(target: Path, signing_identity: str) -> str:
    result = _require_ok(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(target)],
        "could not inspect distribution code signature",
    )
    detail = result.stdout + "\n" + result.stderr
    authorities = re.findall(r"(?m)^Authority=(.+)$", detail)
    if not authorities or authorities[0].strip() != signing_identity:
        raise DistributionVerificationError("distribution code signature does not use the requested Developer ID Application identity")
    team_match = re.search(r"(?m)^TeamIdentifier=(.+)$", detail)
    if team_match is None or not team_match.group(1).strip():
        raise DistributionVerificationError("distribution code signature has no TeamIdentifier")
    flags_match = re.search(r"(?m)^flags=(.+)$", detail)
    if flags_match is None or "runtime" not in flags_match.group(1).lower():
        raise DistributionVerificationError("distribution code signature is missing hardened runtime")
    timestamp_match = re.search(r"(?m)^Timestamp=(.+)$", detail)
    if timestamp_match is None or not timestamp_match.group(1).strip():
        raise DistributionVerificationError("distribution code signature is missing a trusted timestamp")
    return team_match.group(1).strip()


def _distribution_code_targets(app: Path) -> tuple[Path, ...]:
    runtime_venv = app / "Contents" / "Resources" / "runtime" / ".venv"
    python = app / RUNTIME_PYTHON
    native_extensions = tuple(
        sorted(
            (
                path
                for path in runtime_venv.rglob("*")
                if path.suffix in {".so", ".dylib"} and path.is_file()
            ),
            key=lambda path: path.as_posix(),
        )
    )
    return (
        *(app / relative for relative in RESPONSIBLE_EXECUTABLES),
        python,
        *native_extensions,
    )


def verify_distribution(
    app: Path,
    *,
    signing_identity: str,
    runtime_revision: str,
    runtime_tree: str,
    requirements_lock: Path,
) -> dict[str, object]:
    signing_identity = _validated_signing_identity(signing_identity)
    if app.is_symlink() or not app.is_dir():
        raise DistributionVerificationError("distribution app must be a regular directory")
    if requirements_lock.is_symlink() or not requirements_lock.is_file():
        raise DistributionVerificationError("requirements.lock must be a regular non-symlink file")

    _require_ok(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        "distribution candidate failed strict code-signature verification",
    )

    expected_team: str | None = None
    for target in (app, *_distribution_code_targets(app)):
        if target != app and (target.is_symlink() or not target.is_file()):
            raise DistributionVerificationError("distribution executable graph is incomplete")
        team = _signature_identity(target, signing_identity)
        if expected_team is None:
            expected_team = team
        elif team != expected_team:
            raise DistributionVerificationError("distribution executable TeamIdentifier mismatch")

    _require_ok(
        ["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=4", str(app)],
        "Gatekeeper assessment rejected the distribution candidate",
    )
    _require_ok(
        ["/usr/bin/xcrun", "stapler", "validate", str(app)],
        "stapler validation rejected the distribution candidate",
    )

    embedded = provenance.embedded_candidate_identity(app)
    lock_sha = provenance.lock_sha256(requirements_lock)
    if embedded.get("source_revision") != runtime_revision:
        raise DistributionVerificationError("embedded Runtime revision changed during distribution processing")
    if embedded.get("source_tree") != runtime_tree:
        raise DistributionVerificationError("embedded Runtime tree changed during distribution processing")
    if embedded.get("requirements_lock_sha256") != lock_sha:
        raise DistributionVerificationError("embedded Runtime requirements provenance changed during distribution processing")
    if embedded.get("team_identifier") != expected_team:
        raise DistributionVerificationError("embedded Runtime TeamIdentifier does not match distribution signing identity")
    return embedded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--signing-identity", required=True)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--runtime-tree", required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_distribution(
            args.app,
            signing_identity=args.signing_identity,
            runtime_revision=args.runtime_revision,
            runtime_tree=args.runtime_tree,
            requirements_lock=args.requirements_lock,
        )
    except (DistributionVerificationError, provenance.PackageProvenanceError) as exc:
        print("DISTRIBUTION VERIFY ERROR: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
