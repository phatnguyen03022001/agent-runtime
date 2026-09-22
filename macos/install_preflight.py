#!/usr/bin/env python3
from __future__ import annotations

"""Read-only installation prerequisite and operator-state preflight."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_cutover
import runtime_config

EXPECTED_ORIGINS = {
    "https://github.com/phatnguyen03022001/agent-runtime.git",
    "git@github.com:phatnguyen03022001/agent-runtime.git",
}
EXPECTED_TUNNEL_FINGERPRINT = "6aa2b81d6dd8"
RUNTIME_ENV_RELATIVE = Path("Library/Application Support/Agent Runtime/runtime.env")
CUTOVER_RELATIVE = Path("Library/Application Support/Agent Runtime/cutover-transaction")
APP_RELATIVE = Path("Applications/Agent Runtime.app")
LEGACY_TUNNEL_RELATIVE = Path(".config/tunnel-client/agent-runtime.yaml")
REQUIRED_COMMANDS = ("git", "launchctl", "lsof", "curl", "xcrun")
TRACKED_CONFIG_KEYS = (
    "CONTROL_PLANE_API_KEY",
    "CONTROL_PLANE_TUNNEL_ID",
    "AGENT_RUNTIME_WORKSPACE_ROOT",
    "AGENT_RUNTIME_GIT_NAME",
    "AGENT_RUNTIME_GIT_EMAIL",
    "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS",
    "AGENT_RUNTIME_MAX_PARALLELISM",
)
ACTION_SAFE = "SAFE_AUTOMATED"
ACTION_HUMAN = "HUMAN_ACTION_REQUIRED"
ACTION_STOP = "STOP_AND_ESCALATE"


def _check(
    check_id: str,
    status: str,
    reason_code: str,
    message: str,
    *,
    action_class: str | None = None,
    evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": check_id,
        "status": status,
        "reason_code": reason_code,
        "message": message,
    }
    if status != "pass":
        item["action_class"] = action_class or ACTION_STOP
    if evidence:
        item["evidence"] = dict(evidence)
    return item


def _run(argv: list[str], *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            env=None if env is None else dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 127, "", "")


def _read_config(path: Path, *, require_mode: bool) -> tuple[dict[str, str] | None, str | None]:
    try:
        _, _, values = runtime_config._read(path, require_mode=require_mode)
    except SystemExit:
        return None, "configuration file is malformed or unsafe"
    return values, None


def _valid_identity(values: Mapping[str, str]) -> bool:
    try:
        return runtime_config._validated_identity(dict(values), required=True) is not None
    except SystemExit:
        return False


def _valid_limits(values: Mapping[str, str]) -> bool:
    try:
        runtime_config._validate_values(dict(values))
    except SystemExit:
        return False
    return True


def _packaging_python_check(root: Path, environ: Mapping[str, str]) -> tuple[bool, str]:
    script = root / "macos" / "packaging_python.sh"
    if script.is_symlink() or not script.is_file():
        return False, "canonical packaging interpreter authority is unavailable"
    command = 'source "$1"; resolve_packaging_python "PREFLIGHT"'
    result = _run(
        ["/bin/bash", "-c", command, "agent-runtime-preflight", str(script)],
        env=environ,
    )
    if result.returncode != 0:
        return False, "canonical CPython 3.13 packaging interpreter is unavailable or invalid"
    resolved = result.stdout.strip()
    if not resolved.startswith("/") or "\n" in resolved:
        return False, "canonical packaging interpreter resolution was not deterministic"
    return True, "canonical CPython 3.13 packaging interpreter is ready"


def _effective_values(
    checkout: Mapping[str, str] | None,
    canonical: Mapping[str, str] | None,
    environ: Mapping[str, str],
    derived_workspace: Path,
) -> dict[str, str]:
    if canonical is not None:
        return dict(canonical)
    values = {} if checkout is None else dict(checkout)
    for key in (
        "CONTROL_PLANE_API_KEY",
        "CONTROL_PLANE_TUNNEL_ID",
        "AGENT_RUNTIME_GIT_NAME",
        "AGENT_RUNTIME_GIT_EMAIL",
    ):
        if not values.get(key) and environ.get(key):
            values[key] = environ[key]
    values["AGENT_RUNTIME_WORKSPACE_ROOT"] = str(derived_workspace)
    return values


def collect_report(
    root: Path,
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    root = root.resolve()
    home = home.resolve()
    env = dict(os.environ if environ is None else environ)
    checks: list[dict[str, object]] = []

    system = platform.system().lower()
    machine = platform.machine().lower()
    platform_ok = system == "darwin" and machine == "arm64"
    checks.append(
        _check(
            "platform",
            "pass" if platform_ok else "fail",
            "OK" if platform_ok else "UNSUPPORTED_PLATFORM",
            "macOS arm64 package target is available." if platform_ok else "Agent Runtime packaging is qualified only for macOS arm64.",
            action_class=ACTION_HUMAN,
            evidence={"system": system, "architecture": machine},
        )
    )

    git_root = _run(["/usr/bin/git", "-C", str(root), "rev-parse", "--show-toplevel"])
    origin = _run(["/usr/bin/git", "-C", str(root), "config", "--get", "remote.origin.url"])
    repo_ok = (
        git_root.returncode == 0
        and Path(git_root.stdout.strip()).resolve() == root
        and origin.returncode == 0
        and origin.stdout.strip() in EXPECTED_ORIGINS
    )
    checks.append(
        _check(
            "repository",
            "pass" if repo_ok else "fail",
            "OK" if repo_ok else "REPOSITORY_IDENTITY_INVALID",
            "Canonical repository root and origin identity are valid." if repo_ok else "Run preflight from the canonical agent-runtime clone with the exact origin.",
            action_class=ACTION_STOP,
        )
    )

    missing_commands = [name for name in REQUIRED_COMMANDS if shutil.which(name, path=env.get("PATH")) is None]
    checks.append(
        _check(
            "system_commands",
            "pass" if not missing_commands else "fail",
            "OK" if not missing_commands else "REQUIRED_COMMAND_UNAVAILABLE",
            "Required Git and macOS system commands are available." if not missing_commands else "One or more required system commands are unavailable.",
            action_class=ACTION_HUMAN,
            evidence={"missing_count": len(missing_commands)},
        )
    )

    packaging_ok, packaging_message = _packaging_python_check(root, env)
    checks.append(
        _check(
            "packaging_python",
            "pass" if packaging_ok else "fail",
            "OK" if packaging_ok else "PACKAGING_PYTHON_UNAVAILABLE",
            packaging_message,
            action_class=ACTION_HUMAN,
        )
    )

    swift_probe = _run(["/usr/bin/xcrun", "--find", "swift"])
    swift_ok = swift_probe.returncode == 0 and swift_probe.stdout.strip().startswith("/")
    checks.append(
        _check(
            "developer_toolchain",
            "pass" if swift_ok else "fail",
            "OK" if swift_ok else "DEVELOPER_TOOLCHAIN_UNAVAILABLE",
            "Xcode/Swift toolchain is available." if swift_ok else "Install or select Apple developer tools that provide xcrun and Swift.",
            action_class=ACTION_HUMAN,
        )
    )

    derived_workspace = root.parent.resolve()
    workspace_ok = derived_workspace.is_absolute() and derived_workspace.is_dir()
    checks.append(
        _check(
            "workspace",
            "pass" if workspace_ok else "fail",
            "OK" if workspace_ok else "WORKSPACE_INVALID",
            "Derived workspace is an existing absolute directory." if workspace_ok else "Derived workspace is unavailable or invalid.",
            action_class=ACTION_HUMAN,
        )
    )

    checkout_path = root / ".env"
    checkout_values: dict[str, str] | None = None
    if checkout_path.exists() or checkout_path.is_symlink():
        checkout_values, checkout_error = _read_config(checkout_path, require_mode=False)
        checkout_ok = checkout_values is not None
        checkout_reason = "OK" if checkout_ok else "CHECKOUT_CONFIG_INVALID"
        checkout_message = "Checkout configuration syntax is valid." if checkout_ok else checkout_error or "Checkout configuration is invalid."
    else:
        checkout_ok = False
        checkout_reason = "CHECKOUT_CONFIG_MISSING"
        checkout_message = "Create checkout .env from .env.example and provide required operator values."
    checks.append(
        _check(
            "checkout_config",
            "pass" if checkout_ok else "fail",
            checkout_reason,
            checkout_message,
            action_class=ACTION_HUMAN,
        )
    )

    canonical_path = home / RUNTIME_ENV_RELATIVE
    canonical_values: dict[str, str] | None = None
    if canonical_path.exists() or canonical_path.is_symlink():
        canonical_values, canonical_error = _read_config(canonical_path, require_mode=True)
        canonical_ok = canonical_values is not None
        canonical_reason = "OK" if canonical_ok else "CANONICAL_CONFIG_INVALID"
        canonical_message = "Canonical runtime.env is a safe mode-0600 configuration." if canonical_ok else canonical_error or "Canonical runtime.env is invalid."
        canonical_status = "pass" if canonical_ok else "fail"
    else:
        canonical_ok = True
        canonical_reason = "OK"
        canonical_message = "Canonical runtime.env is absent and will be initialized atomically by installation."
        canonical_status = "pass"
    checks.append(
        _check(
            "canonical_config",
            canonical_status,
            canonical_reason,
            canonical_message,
            action_class=ACTION_STOP,
            evidence={"present": canonical_path.exists() or canonical_path.is_symlink()},
        )
    )

    effective = _effective_values(checkout_values, canonical_values, env, derived_workspace)
    required_values_ready = all(effective.get(key) for key in runtime_config.REQUIRED)
    checks.append(
        _check(
            "runtime_configuration",
            "pass" if required_values_ready else "fail",
            "OK" if required_values_ready else "RUNTIME_CONFIGURATION_INCOMPLETE",
            "Required Runtime and OpenAI transport settings are present." if required_values_ready else "Required Runtime/OpenAI settings are missing.",
            action_class=ACTION_HUMAN,
        )
    )

    identity_ready = _valid_identity(effective)
    if not identity_ready and canonical_values is None:
        git_name = _run(["/usr/bin/git", "-C", str(root), "config", "--local", "--get", "user.name"])
        git_email = _run(["/usr/bin/git", "-C", str(root), "config", "--local", "--get", "user.email"])
        if git_name.returncode == 0 and git_email.returncode == 0:
            identity_ready = (
                runtime_config._identity_value_valid(git_name.stdout.rstrip("\r\n"))
                and runtime_config._identity_value_valid(git_email.stdout.rstrip("\r\n"))
            )
    checks.append(
        _check(
            "git_identity",
            "pass" if identity_ready else "fail",
            "OK" if identity_ready else "GIT_IDENTITY_UNAVAILABLE",
            "Runtime Git identity is ready without exposing its values." if identity_ready else "Provide a valid Runtime Git name/email pair explicitly or in repository-local Git config.",
            action_class=ACTION_HUMAN,
        )
    )

    limits_ok = _valid_limits(effective) if required_values_ready else False
    checks.append(
        _check(
            "runtime_limits",
            "pass" if limits_ok else "fail",
            "OK" if limits_ok else "RUNTIME_LIMIT_CONFIG_INVALID",
            "Runtime parallelism and session settings are within accepted bounds." if limits_ok else "Runtime parallelism/session settings or workspace value are invalid.",
            action_class=ACTION_HUMAN,
        )
    )

    tunnel_client = shutil.which("tunnel-client", path=env.get("PATH"))
    tunnel_executable_ok = False
    if tunnel_client:
        tunnel_path = Path(tunnel_client)
        tunnel_executable_ok = tunnel_path.is_absolute() and tunnel_path.is_file() and os.access(tunnel_path, os.X_OK)
    tunnel_id = effective.get("CONTROL_PLANE_TUNNEL_ID", "")
    fingerprint_ok = bool(tunnel_id) and hashlib.sha256(tunnel_id.encode("utf-8")).hexdigest()[:12] == EXPECTED_TUNNEL_FINGERPRINT
    tunnel_ok = tunnel_executable_ok and fingerprint_ok
    tunnel_reason = "OK"
    tunnel_message = "Official tunnel-client path and accepted tunnel identity are ready."
    if not tunnel_executable_ok:
        tunnel_reason = "TUNNEL_CLIENT_UNAVAILABLE"
        tunnel_message = "Install the official OpenAI tunnel-client before installation."
    elif not fingerprint_ok:
        tunnel_reason = "TUNNEL_IDENTITY_MISMATCH"
        tunnel_message = "Configured tunnel identity does not match the accepted Runtime tunnel fingerprint."
    checks.append(
        _check(
            "openai_tunnel",
            "pass" if tunnel_ok else "fail",
            tunnel_reason,
            tunnel_message,
            action_class=ACTION_HUMAN,
            evidence={"executable_ready": tunnel_executable_ok, "fingerprint_match": fingerprint_ok},
        )
    )

    legacy = home / LEGACY_TUNNEL_RELATIVE
    legacy_absent = not legacy.exists() and not legacy.is_symlink()
    checks.append(
        _check(
            "legacy_tunnel_profile",
            "pass" if legacy_absent else "fail",
            "OK" if legacy_absent else "LEGACY_TUNNEL_PROFILE_PRESENT",
            "No legacy tunnel profile is present." if legacy_absent else "Legacy tunnel profile must be removed through an explicit operator decision before installation.",
            action_class=ACTION_HUMAN,
        )
    )

    app = home / APP_RELATIVE
    transaction = home / CUTOVER_RELATIVE
    app_present = app.exists() or app.is_symlink()
    app_safe = not app_present or (app.is_dir() and not app.is_symlink())
    transaction_present = transaction.exists() or transaction.is_symlink()
    transaction_valid = True
    if transaction_present:
        try:
            candidate_cutover._load_metadata(
                transaction,
                allowed_schemas={
                    candidate_cutover.SCHEMA4_ROLLBACK_SCHEMA,
                    candidate_cutover.TRANSACTION_SCHEMA,
                },
            )
        except Exception:
            transaction_valid = False

    if not app_safe:
        installed_status = "fail"
        installed_reason = "INSTALLED_PACKAGE_INVALID"
        installed_message = "Installed app path is unsafe or ambiguous."
        installed_action = ACTION_STOP
    elif transaction_present and not transaction_valid:
        installed_status = "fail"
        installed_reason = "CUTOVER_STATE_INVALID"
        installed_message = "Existing cutover transaction metadata is malformed or ambiguous."
        installed_action = ACTION_STOP
    elif transaction_present:
        installed_status = "fail"
        installed_reason = "CUTOVER_TRANSACTION_PRESENT"
        installed_message = "A recognized cutover transaction is already present; choose resume, commit, rollback, or recovery before a new install."
        installed_action = ACTION_HUMAN
    else:
        installed_status = "pass"
        installed_reason = "OK"
        installed_message = "Installed app/cutover state does not block a new installation transaction."
        installed_action = None
    checks.append(
        _check(
            "installed_state",
            installed_status,
            installed_reason,
            installed_message,
            action_class=installed_action,
            evidence={"app_present": app_present, "cutover_present": transaction_present},
        )
    )

    service_status = "pass"
    service_reason = "OK"
    service_message = "No installed app requires ServiceManagement inspection."
    service_action = None
    service_evidence: dict[str, object] = {"app_present": app_present}
    if app_safe and app_present:
        try:
            service = candidate_cutover._service_management(app, "status")
            main_state = service.get("main_app")
            runtime_state = service.get("runtime_agent")
            service_evidence = {
                "app_present": True,
                "main_app": main_state,
                "runtime_agent": runtime_state,
            }
            if "requires-approval" in {main_state, runtime_state}:
                service_status = "fail"
                service_reason = "SERVICE_APPROVAL_REQUIRED"
                service_message = "Existing ServiceManagement registration requires operator approval."
                service_action = ACTION_HUMAN
            elif main_state == "enabled" and runtime_state == "enabled":
                service_message = "Existing ServiceManagement registration is enabled."
            elif main_state in {"not-registered", "not-found"} or runtime_state in {"not-registered", "not-found"}:
                service_status = "fail"
                service_reason = "SERVICE_NOT_REGISTERED"
                service_message = "Existing installed app has incomplete ServiceManagement registration."
                service_action = ACTION_STOP
            else:
                service_status = "fail"
                service_reason = "SERVICE_STATUS_INVALID"
                service_message = "Existing ServiceManagement state is not recognized."
                service_action = ACTION_STOP
        except Exception:
            service_status = "fail"
            service_reason = "SERVICE_STATUS_INVALID"
            service_message = "Existing ServiceManagement state could not be inspected safely."
            service_action = ACTION_STOP
    checks.append(
        _check(
            "service_management",
            service_status,
            service_reason,
            service_message,
            action_class=service_action,
            evidence=service_evidence,
        )
    )

    signing_ready = bool(env.get("AGENT_RUNTIME_CODESIGN_IDENTITY", "").strip())
    checks.append(
        _check(
            "signing_prerequisite",
            "pass" if signing_ready else "fail",
            "OK" if signing_ready else "SIGNING_IDENTITY_REQUIRED",
            "Explicit signing identity is supplied by the operator." if signing_ready else "Set AGENT_RUNTIME_CODESIGN_IDENTITY to an explicit non-ad-hoc identity; preflight never enumerates Keychain identities.",
            action_class=ACTION_HUMAN,
        )
    )

    status = "ready" if all(item["status"] == "pass" for item in checks) else "blocked"
    return {
        "schema_version": 1,
        "status": status,
        "checks": checks,
    }


def render_human(report: Mapping[str, object]) -> str:
    lines = [f"Agent Runtime install preflight: {str(report['status']).upper()}"]
    for raw in report["checks"]:
        check = dict(raw)
        suffix = ""
        if check["status"] != "pass":
            suffix = f" [{check['action_class']}]"
        lines.append(f"{check['id']}: {check['status']} ({check['reason_code']}){suffix} - {check['message']}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="./install.sh --check")
    parser.add_argument("--json", action="store_true", help="emit deterministic JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    home = Path.home()
    report = collect_report(root, home)
    output = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if args.json else render_human(report)
    sys.stdout.write(output + "\n")
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
