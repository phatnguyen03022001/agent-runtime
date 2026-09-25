from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from macos import candidate_cutover, package_provenance

from . import protection, server
from .capability_registry import (
    ADVERTISED_TOOL_NAMES,
    CAPABILITY_NAMES,
    TOOL_CONTRACT_KERNEL_VERSION,
    capability_descriptors,
    runtime_capabilities_result,
)
from .capacity import DEFAULT_MAX_PARALLELISM, MAX_PARALLELISM_ENV
from .contracts import DoctorCheck, DoctorReport
from .schema_export import build_schema_bundle
from .session import (
    DEFAULT_SESSION_LIMIT,
    MAX_ACTIVE_SESSIONS,
    SESSION_LIMIT_ENV,
    terminal_recovery_reason,
)
from .version import RUNTIME_VERSION

_CHECK_IDS = (
    "runtime_identity",
    "capability_registry",
    "tool_contract_schema",
    "workspace",
    "capacity_session_config",
    "git_readiness",
    "installed_package_identity",
    "service_registration",
    "cutover_identity",
    "governance_protection",
)
_MAX_EVIDENCE_BYTES = 4096
_MAX_PACKAGE_METADATA_BYTES = 1024 * 1024
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")


def _check(
    check_id: str,
    status: str,
    reason_code: str,
    message: str,
    evidence: dict[str, object] | None = None,
) -> DoctorCheck:
    payload = {} if evidence is None else evidence
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_EVIDENCE_BYTES:
        raise ValueError("doctor evidence exceeds fixed bound")
    return DoctorCheck(
        id=check_id,
        status=status,
        reason_code=reason_code,
        message=message,
        evidence=payload,
    )


def _compose_status(checks: list[DoctorCheck]) -> str:
    if any(check.status == "fail" for check in checks):
        return "unhealthy"
    if any(check.status == "warn" for check in checks):
        return "degraded"
    return "healthy"


def _schema_bundle() -> dict[str, object] | None:
    try:
        return asyncio.run(build_schema_bundle())
    except Exception:
        return None


def _runtime_identity_check(bundle: dict[str, object] | None) -> DoctorCheck:
    capabilities = runtime_capabilities_result()
    observed = (
        RUNTIME_VERSION,
        server.mcp.version,
        capabilities.runtime_version,
        None if bundle is None else bundle.get("runtime_version"),
    )
    if bundle is None or any(value != RUNTIME_VERSION for value in observed):
        return _check(
            "runtime_identity",
            "fail",
            "RUNTIME_IDENTITY_MISMATCH",
            "Runtime identity sources do not agree.",
            {"runtime_version": RUNTIME_VERSION, "source_count": 4},
        )
    return _check(
        "runtime_identity",
        "pass",
        "OK",
        "Runtime identity sources agree.",
        {"runtime_version": RUNTIME_VERSION, "source_count": 4},
    )


def _capability_registry_check() -> DoctorCheck:
    descriptors = capability_descriptors()
    names = tuple(item.name for item in descriptors)
    advertised = tuple(item.name for item in descriptors if item.advertised)
    available_count = sum(item.available for item in descriptors)
    screen = next((item for item in descriptors if item.name == "screen_capture"), None)
    valid = (
        len(names) == 21
        and names == CAPABILITY_NAMES
        and advertised == ADVERTISED_TOOL_NAMES
        and advertised == server.PUBLIC_TOOL_NAMES
        and len(advertised) == 20
        and len(set(names)) == 21
        and available_count == 20
        and len(descriptors) - available_count == 1
        and screen is not None
        and screen.supported
        and not screen.available
        and not screen.advertised
        and screen.unavailable_reason_code == "VISUAL_PERCEPTION_BLOCKED"
    )
    evidence = {
        "capability_count": len(names),
        "advertised_tool_count": len(advertised),
        "available_count": available_count,
        "unavailable_count": len(descriptors) - available_count,
        "screen_capture": "VISUAL_PERCEPTION_BLOCKED",
    }
    if not valid:
        return _check(
            "capability_registry",
            "fail",
            "CAPABILITY_REGISTRY_MISMATCH",
            "Known and advertised capability inventories do not match accepted authority.",
            evidence,
        )
    return _check(
        "capability_registry",
        "pass",
        "OK",
        "Capability registry exposes twenty advertised tools from twenty-one known capabilities.",
        evidence,
    )


def _tool_contract_schema_check(bundle: dict[str, object] | None) -> DoctorCheck:
    if bundle is None:
        return _check(
            "tool_contract_schema",
            "fail",
            "SCHEMA_EXPORT_INVALID",
            "Canonical schema export could not be validated.",
        )
    entries = bundle.get("capabilities")
    if not isinstance(entries, list) or len(entries) != 21:
        return _check(
            "tool_contract_schema",
            "fail",
            "SCHEMA_EXPORT_INVALID",
            "Canonical schema export has an unexpected capability inventory.",
        )
    try:
        names = tuple(entry["descriptor"]["name"] for entry in entries)
        kernels = tuple(entry["descriptor"]["tool_contract_kernel_version"] for entry in entries)
        advertised = tuple(
            entry["descriptor"]["name"]
            for entry in entries
            if entry["descriptor"]["advertised"]
        )
        hidden = tuple(
            entry["descriptor"]["name"]
            for entry in entries
            if not entry["descriptor"]["advertised"]
        )
        registered_resultless = tuple(
            entry["descriptor"]["name"]
            for entry in entries
            if entry["descriptor"]["advertised"] and entry["result_schema"] is None
        )
        hidden_entries = [
            entry
            for entry in entries
            if not entry["descriptor"]["advertised"]
        ]
        digest = bundle["bundle_sha256"]
        schema_versions = {
            entry["descriptor"]["name"]: (
                entry["descriptor"]["request_schema_version"],
                entry["descriptor"]["result_schema_version"],
            )
            for entry in entries
        }
    except (KeyError, TypeError):
        return _check(
            "tool_contract_schema",
            "fail",
            "SCHEMA_EXPORT_INVALID",
            "Canonical schema export is malformed.",
        )
    valid = (
        names == CAPABILITY_NAMES
        and advertised == ADVERTISED_TOOL_NAMES
        and advertised == server.PUBLIC_TOOL_NAMES
        and hidden == ("screen_capture",)
        and registered_resultless == ()
        and len(hidden_entries) == 1
        and hidden_entries[0]["request_schema"] is None
        and hidden_entries[0]["result_schema"] is None
        and all(value == TOOL_CONTRACT_KERNEL_VERSION for value in kernels)
        and schema_versions.get("terminal_start") == (4, 4)
        and schema_versions.get("terminal_poll") == (4, 4)
        and schema_versions.get("capacity_observer") == (1, 2)
        and schema_versions.get("runtime_capabilities") == (2, 2)
        and schema_versions.get("fs_read_batch") == (1, 2)
        and schema_versions.get("fs_manage") == (1, 1)
        and schema_versions.get("repo_remote_observer") == (1, 1)
        and isinstance(digest, str)
        and _HEX64.fullmatch(digest) is not None
    )
    if not valid:
        return _check(
            "tool_contract_schema",
            "fail",
            "TOOL_CONTRACT_SCHEMA_MISMATCH",
            "ToolContract or registered schema projection does not match accepted authority.",
            {"tool_count": len(entries)},
        )
    return _check(
        "tool_contract_schema",
        "pass",
        "OK",
        "ToolContract Kernel and registered schemas match accepted authority.",
        {
            "capability_count": len(entries),
            "advertised_tool_count": len(advertised),
            "tool_contract_kernel_version": TOOL_CONTRACT_KERNEL_VERSION,
            "bundle_sha256": digest,
        },
    )

def _workspace_check(environ: Mapping[str, str]) -> DoctorCheck:
    raw = environ.get("AGENT_RUNTIME_WORKSPACE_ROOT")
    if not raw:
        return _check(
            "workspace",
            "fail",
            "WORKSPACE_UNAVAILABLE",
            "AGENT_RUNTIME_WORKSPACE_ROOT is not configured.",
        )
    path = Path(raw)
    if not path.is_absolute():
        return _check(
            "workspace",
            "fail",
            "WORKSPACE_INVALID",
            "Configured workspace root is not absolute.",
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return _check(
            "workspace",
            "fail",
            "WORKSPACE_INVALID",
            "Configured workspace root cannot be resolved safely.",
        )
    if not resolved.is_dir():
        return _check(
            "workspace",
            "fail",
            "WORKSPACE_INVALID",
            "Configured workspace root is not a directory.",
        )
    return _check(
        "workspace",
        "pass",
        "OK",
        "Configured workspace root is an existing absolute directory.",
        {"absolute": True, "existing_directory": True},
    )


def _parse_bounded_integer(
    raw: str | None,
    *,
    default: int,
    maximum: int,
) -> tuple[bool, int | None]:
    if raw is None:
        return True, default
    if _POSITIVE_INTEGER.fullmatch(raw) is None:
        return False, None
    value = int(raw)
    if not 1 <= value <= maximum:
        return False, None
    return True, value


def _capacity_session_config_check(environ: Mapping[str, str]) -> DoctorCheck:
    parallel_ok, parallelism = _parse_bounded_integer(
        environ.get(MAX_PARALLELISM_ENV),
        default=DEFAULT_MAX_PARALLELISM,
        maximum=10,
    )
    session_ok, sessions = _parse_bounded_integer(
        environ.get(SESSION_LIMIT_ENV),
        default=DEFAULT_SESSION_LIMIT,
        maximum=MAX_ACTIVE_SESSIONS,
    )
    recovery_reason = terminal_recovery_reason()
    if recovery_reason is not None:
        return _check(
            "capacity_session_config",
            "fail",
            recovery_reason,
            "Durable Runtime recovery is not ready for new terminal execution.",
            {"durable_recovery_ready": False},
        )
    if not parallel_ok or not session_ok:
        return _check(
            "capacity_session_config",
            "fail",
            "RUNTIME_LIMIT_CONFIG_INVALID",
            "Configured Runtime parallelism or session limit is outside accepted bounds.",
            {
                "parallelism_valid": parallel_ok,
                "session_limit_valid": session_ok,
            },
        )
    return _check(
        "capacity_session_config",
        "pass",
        "OK",
        "Runtime parallelism and session limits are within accepted bounds.",
        {
            "max_parallelism": parallelism,
            "max_active_sessions": sessions,
        },
    )


def _identity_value_valid(value: str | None) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return len(raw) <= 256 and not any(ch in value for ch in ("\x00", "\r", "\n"))


def _git_readiness_check(environ: Mapping[str, str]) -> DoctorCheck:
    git = Path("/usr/bin/git")
    if git.is_symlink() or not git.is_file() or not os.access(git, os.X_OK):
        return _check(
            "git_readiness",
            "fail",
            "GIT_UNAVAILABLE",
            "Fixed /usr/bin/git executable is unavailable.",
        )
    try:
        result = subprocess.run(
            [str(git), "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return _check(
            "git_readiness",
            "fail",
            "GIT_UNAVAILABLE",
            "Fixed /usr/bin/git readiness probe failed.",
        )
    identity_ok = _identity_value_valid(environ.get("AGENT_RUNTIME_GIT_NAME")) and _identity_value_valid(
        environ.get("AGENT_RUNTIME_GIT_EMAIL")
    )
    if result.returncode != 0:
        return _check(
            "git_readiness",
            "fail",
            "GIT_UNAVAILABLE",
            "Fixed /usr/bin/git readiness probe failed.",
        )
    if not identity_ok:
        return _check(
            "git_readiness",
            "fail",
            "GIT_IDENTITY_UNAVAILABLE",
            "Runtime Git identity is missing or malformed.",
            {"git_available": True, "identity_valid": False},
        )
    return _check(
        "git_readiness",
        "pass",
        "OK",
        "Fixed Git executable and Runtime Git identity are ready.",
        {"git_available": True, "identity_valid": True},
    )


def _installed_package_check(app: Path) -> DoctorCheck:
    if not app.exists() and not app.is_symlink():
        return _check(
            "installed_package_identity",
            "not_applicable",
            "APP_NOT_INSTALLED",
            "Canonical Agent Runtime app is not installed.",
        )
    if app.is_symlink() or not app.is_dir():
        return _check(
            "installed_package_identity",
            "fail",
            "INSTALLED_PACKAGE_INVALID",
            "Canonical installed app path is unsafe.",
        )

    info_path = app / "Contents" / "Info.plist"
    manifest_path = app / "Contents" / "Resources" / "runtime-manifest.json"
    runtime_path = app / "Contents" / "Resources" / "runtime"
    try:
        if (
            info_path.is_symlink()
            or not info_path.is_file()
            or info_path.stat().st_size > _MAX_PACKAGE_METADATA_BYTES
        ):
            raise ValueError("unsafe Info.plist")
        info = plistlib.loads(info_path.read_bytes())
        if (
            info.get("CFBundleIdentifier") != package_provenance.OWNER
            or info.get("CFBundleExecutable") != "AgentRuntimeMenuBar"
            or info.get("CFBundleShortVersionString") != RUNTIME_VERSION
        ):
            raise ValueError("foreign package identity")
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > _MAX_PACKAGE_METADATA_BYTES
        ):
            raise ValueError("unsafe runtime manifest")
        manifest = package_provenance._load_manifest(manifest_path)
        revision = manifest.get("runtime_revision")
        tree = manifest.get("git_tree")
        lock_sha = manifest.get("requirements_lock_sha256")
        payload_sha = manifest.get("payload_sha256")
        files = manifest.get("files")
        if (
            manifest.get("schema") != package_provenance.SCHEMA
            or manifest.get("owner") != package_provenance.OWNER
            or manifest.get("entrypoint") != "runtime/start.sh"
            or manifest.get("python") != "runtime/.venv/bin/python"
            or manifest.get("mcp_package") != "runtime/agent_runtime"
            or not isinstance(revision, str)
            or _HEX40.fullmatch(revision) is None
            or not isinstance(tree, str)
            or _HEX40.fullmatch(tree) is None
            or not isinstance(lock_sha, str)
            or _HEX64.fullmatch(lock_sha) is None
            or not isinstance(payload_sha, str)
            or _HEX64.fullmatch(payload_sha) is None
            or not isinstance(files, list)
            or runtime_path.is_symlink()
            or not runtime_path.is_dir()
        ):
            raise ValueError("runtime manifest identity mismatch")
        for entry in files:
            if (
                not isinstance(entry, dict)
                or set(entry) != package_provenance.ENTRY_KEYS
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("size"), int)
                or entry["size"] < 0
                or not isinstance(entry.get("sha256"), str)
                or _HEX64.fullmatch(entry["sha256"]) is None
            ):
                raise ValueError("runtime manifest file metadata mismatch")
        package_provenance.responsible_code_identity(app)
        package_provenance._verify_codesign(app)
    except Exception:
        return _check(
            "installed_package_identity",
            "fail",
            "INSTALLED_PACKAGE_INVALID",
            "Installed Agent Runtime package identity is invalid.",
        )

    return _check(
        "installed_package_identity",
        "pass",
        "OK",
        "Installed Agent Runtime package identity is valid.",
        {
            "bundle_identifier": package_provenance.OWNER,
            "runtime_version": RUNTIME_VERSION,
            "runtime_revision": revision,
            "git_tree": tree,
            "codesign_consistent": True,
        },
    )


def _service_registration_check(app: Path) -> DoctorCheck:
    if not app.exists() and not app.is_symlink():
        return _check(
            "service_registration",
            "not_applicable",
            "APP_NOT_INSTALLED",
            "Service registration is not applicable without the canonical app.",
        )
    try:
        state = candidate_cutover._service_management(app, "status")
    except Exception:
        return _check(
            "service_registration",
            "fail",
            "SERVICE_STATUS_INVALID",
            "Canonical read-only ServiceManagement status could not be validated.",
        )
    main_state = state.get("main_app")
    runtime_state = state.get("runtime_agent")
    evidence = {
        "main_app": main_state,
        "runtime_agent": runtime_state,
    }
    if main_state == "enabled" and runtime_state == "enabled":
        return _check(
            "service_registration",
            "pass",
            "OK",
            "Agent Runtime services are registered and enabled.",
            evidence,
        )
    if "requires-approval" in {main_state, runtime_state}:
        return _check(
            "service_registration",
            "warn",
            "SERVICE_APPROVAL_REQUIRED",
            "Agent Runtime service registration requires user approval.",
            evidence,
        )
    return _check(
        "service_registration",
        "warn",
        "SERVICE_NOT_REGISTERED",
        "One or more Agent Runtime services are not registered.",
        evidence,
    )


def _cutover_identity_check(transaction_dir: Path, app: Path) -> DoctorCheck:
    if not transaction_dir.exists() and not transaction_dir.is_symlink():
        return _check(
            "cutover_identity",
            "pass",
            "OK",
            "No cutover transaction is pending.",
            {"transaction_present": False},
        )
    if transaction_dir.is_symlink() or not transaction_dir.is_dir():
        return _check(
            "cutover_identity",
            "fail",
            "CUTOVER_STATE_INVALID",
            "Canonical cutover transaction path is unsafe.",
        )
    try:
        metadata = candidate_cutover._load_metadata(transaction_dir)
        phase = candidate_cutover._validate_transaction_phase(metadata.get("phase"))
        status = metadata.get("status")
        if status not in {"PREPARED", "AWAITING_APPROVAL", "PENDING", "PARTIAL"}:
            raise ValueError("unknown transaction status")
        paths = metadata.get("paths")
        if not isinstance(paths, dict) or Path(str(paths.get("target_app", ""))) != app:
            raise ValueError("target path contradiction")
        if metadata.get("modern_runtime_label") != candidate_cutover.MODERN_RUNTIME_LABEL:
            raise ValueError("runtime label contradiction")
        if status == "PREPARED" and phase != "PRE_SWAP":
            raise ValueError("prepared phase contradiction")
        if status in {"AWAITING_APPROVAL", "PENDING"} and phase != "APP_SWAPPED":
            raise ValueError("post-swap phase contradiction")
    except Exception:
        return _check(
            "cutover_identity",
            "fail",
            "CUTOVER_STATE_INVALID",
            "Present cutover transaction metadata is malformed or contradictory.",
            {"transaction_present": True},
        )
    return _check(
        "cutover_identity",
        "warn",
        "CUTOVER_TRANSACTION_PRESENT",
        "A recognized cutover transaction is present.",
        {
            "transaction_present": True,
            "schema": candidate_cutover.TRANSACTION_SCHEMA,
            "status": status,
            "phase": phase,
        },
    )


def _governance_protection_check() -> DoctorCheck:
    descriptors = capability_descriptors()
    screen = next((item for item in descriptors if item.name == "screen_capture"), None)
    guard = protection._PROTECTED_GUARD
    valid = (
        len(CAPABILITY_NAMES) == 21
        and len(ADVERTISED_TOOL_NAMES) == 20
        and screen is not None
        and screen.supported
        and not screen.available
        and not screen.advertised
        and screen.unavailable_reason_code == "VISUAL_PERCEPTION_BLOCKED"
        and isinstance(guard, protection.ProtectedRuntimeGuard)
        and bool(guard.protected_launchd_labels)
    )
    if not valid:
        return _check(
            "governance_protection",
            "fail",
            "GOVERNANCE_PROTECTION_INVALID",
            "Runtime governance or protected-runtime policy does not match accepted authority.",
            {
                "capability_count": len(CAPABILITY_NAMES),
                "advertised_tool_count": len(ADVERTISED_TOOL_NAMES),
            },
        )
    return _check(
        "governance_protection",
        "pass",
        "OK",
        "Runtime governance and protected-runtime policy remain active.",
        {
            "capability_count": len(CAPABILITY_NAMES),
            "advertised_tool_count": len(ADVERTISED_TOOL_NAMES),
            "screen_capture": "VISUAL_PERCEPTION_BLOCKED",
            "protected_runtime": True,
        },
    )


def collect_report(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> DoctorReport:
    observed_home = Path.home() if home is None else Path(home)
    values = os.environ if environ is None else environ
    app = observed_home / "Applications" / "Agent Runtime.app"
    state_dir = observed_home / "Library" / "Application Support" / "Agent Runtime"
    transaction_dir = state_dir / "cutover-transaction"
    bundle = _schema_bundle()

    checks = [
        _runtime_identity_check(bundle),
        _capability_registry_check(),
        _tool_contract_schema_check(bundle),
        _workspace_check(values),
        _capacity_session_config_check(values),
        _git_readiness_check(values),
        _installed_package_check(app),
        _service_registration_check(app),
        _cutover_identity_check(transaction_dir, app),
        _governance_protection_check(),
    ]
    if tuple(check.id for check in checks) != _CHECK_IDS:
        raise RuntimeError("doctor check inventory drift")
    return DoctorReport(
        schema_version=1,
        runtime_version=RUNTIME_VERSION,
        status=_compose_status(checks),
        checks=checks,
    )


def render_json(report: DoctorReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def render_human(report: DoctorReport) -> str:
    lines = [f"Agent Runtime {report.runtime_version}: {report.status}"]
    lines.extend(
        f"{check.id}: {check.status} ({check.reason_code}) - {check.message}"
        for check in report.checks
    )
    return "\n".join(lines)


def exit_code(report: DoctorReport) -> int:
    return 0 if report.status == "healthy" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_runtime.doctor")
    parser.add_argument("--json", action="store_true", help="emit one canonical JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = collect_report()
        rendered = render_json(report) if args.json else render_human(report)
    except Exception:
        if args.json:
            sys.stdout.write(
                '{"error":{"message":"doctor output serialization failed",'
                '"reason_code":"INTERNAL_SERIALIZATION_FAILURE"}}\n'
            )
        else:
            sys.stderr.write("doctor: internal serialization failure\n")
        return 2
    sys.stdout.write(rendered + "\n")
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
