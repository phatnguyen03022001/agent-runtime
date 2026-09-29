#!/usr/bin/env python3
"""Owner-safe uninstall for the per-user Agent Runtime product."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

OWNER = "com.picmao.agent-runtime"
UI_LABEL = "com.picmao.agent-runtime-ui"
LEGACY_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
MODERN_RUNTIME_LABEL = "com.picmao.agent-runtime-runtime-service"
SERVICE_PLIST = f"{MODERN_RUNTIME_LABEL}.plist"
KNOWN_SERVICE_STATES = {"enabled", "requires-approval", "not-registered", "not-found"}
REGISTERED_SERVICE_STATES = {"enabled", "requires-approval"}
ABSENT_SERVICE_STATES = {"not-registered", "not-found"}


class UninstallError(RuntimeError):
    pass


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _load_plist(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise UninstallError(f"ownership is ambiguous for {path}")
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise UninstallError(f"ownership metadata is malformed for {path}") from exc
    if not isinstance(value, dict):
        raise UninstallError(f"ownership metadata is malformed for {path}")
    return value


def _validate_app(app: Path) -> str:
    info = _load_plist(app / "Contents" / "Info.plist")
    if info.get("CFBundleIdentifier") != OWNER or info.get("CFBundleExecutable") != "AgentRuntimeMenuBar":
        raise UninstallError("installed app ownership is ambiguous")
    main = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
    helper = app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
    runtime_start = app / "Contents" / "Resources" / "runtime" / "start.sh"
    for required in (main, helper, runtime_start):
        if required.is_symlink() or not required.is_file():
            raise UninstallError("installed app ownership is incomplete")

    service_plist = app / "Contents" / "Library" / "LaunchAgents" / SERVICE_PLIST
    if not service_plist.exists() and not service_plist.is_symlink():
        return "launchagent-v1"
    service = _load_plist(service_plist)
    if (
        service.get("Label") != MODERN_RUNTIME_LABEL
        or service.get("BundleProgram") != "Contents/MacOS/AgentRuntimeRuntimeService"
    ):
        raise UninstallError("predecessor ServiceManagement ownership is ambiguous")
    return "servicemanagement-predecessor"


def _current_launchagent(path: Path, app: Path, *, uid: int) -> Path:
    value = _load_plist(path)
    helper = app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
    expected = {
        "Label": MODERN_RUNTIME_LABEL,
        "ProgramArguments": [str(helper)],
        "RunAtLoad": False,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "ThrottleInterval": 2,
    }
    info = path.stat()
    if info.st_uid != uid or (info.st_mode & 0o777) != 0o600 or value != expected:
        raise UninstallError("current LaunchAgent ownership is ambiguous")
    return helper


def _validate_payload_state(state_dir: Path, *, uid: int) -> tuple[Path, Path]:
    pointer = state_dir / "current-payload"
    payloads = state_dir / "payloads"
    if pointer.is_symlink() or not pointer.is_file():
        raise UninstallError("current payload pointer ownership is ambiguous")
    info = pointer.stat()
    if info.st_uid != uid or (info.st_mode & 0o777) != 0o600:
        raise UninstallError("current payload pointer ownership is ambiguous")
    try:
        raw = pointer.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise UninstallError("current payload pointer is unreadable") from exc
    if re.fullmatch(r"[0-9a-f]{64}\n", raw) is None:
        raise UninstallError("current payload pointer is malformed")
    if payloads.is_symlink() or not payloads.is_dir():
        raise UninstallError("current payload store ownership is ambiguous")
    selected = raw.strip()
    for release in payloads.iterdir():
        if (
            release.is_symlink()
            or not release.is_dir()
            or re.fullmatch(r"[0-9a-f]{64}", release.name) is None
        ):
            raise UninstallError("payload store contains foreign or ambiguous state")
        manifest_path = release / "payload-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise UninstallError("payload release ownership metadata is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UninstallError("payload release ownership metadata is malformed") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("owner") != OWNER
            or manifest.get("content_closure") != release.name
        ):
            raise UninstallError("payload release ownership is ambiguous")
    if not (payloads / selected).is_dir():
        raise UninstallError("selected payload release is missing")
    return pointer, payloads


def _runtime_lifecycle(app: Path, action: str) -> None:
    if action not in {"start", "stop"}:
        raise UninstallError("Runtime lifecycle action is invalid")
    script = app / "Contents" / "Resources" / "runtime" / "start.sh"
    result = _run(["/bin/bash", str(script), action])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise UninstallError(f"Runtime {action} failed: {detail[:300]}")

def _legacy_program(path: Path, label: str, app: Path) -> Path:
    value = _load_plist(path)
    args = value.get("ProgramArguments")
    if value.get("Label") != label or not isinstance(args, list) or not all(isinstance(v, str) for v in args):
        raise UninstallError(f"legacy ownership is ambiguous for {label}")
    if label == UI_LABEL:
        expected = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
        valid = args == [str(expected)]
    else:
        expected = app / "Contents" / "Resources" / "runtime" / "start.sh"
        valid = (
            len(args) == 3
            and args[0] == str(expected)
            and args[1] == "--serve"
            and Path(args[2]).is_absolute()
            and Path(args[2]).name == "tunnel-client"
        )
    if not valid:
        raise UninstallError(f"legacy ownership is ambiguous for {label}")
    return expected


def _service_snapshot(app: Path, operation: str) -> dict[str, str]:
    executable = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
    result = _run([str(executable), "--service-management", operation])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise UninstallError(f"ServiceManagement {operation} failed: {detail[:300]}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UninstallError("ServiceManagement diagnostics are malformed") from exc
    if not isinstance(value, dict) or set(value) != {"main_app", "runtime_agent"}:
        raise UninstallError("ServiceManagement diagnostics are malformed")
    if any(not isinstance(value[key], str) or value[key] not in KNOWN_SERVICE_STATES for key in value):
        raise UninstallError("ServiceManagement diagnostics contain an unknown state")
    return value


def _legacy_loaded(launchctl: Path, uid: int, label: str, expected_program: Path) -> bool:
    service = f"gui/{uid}/{label}"
    result = _run([str(launchctl), "print", service])
    if result.returncode == 113:
        return False
    if result.returncode != 0:
        raise UninstallError(f"could not prove legacy registration state for {label}")
    match = re.search(r"(?m)^\s*program = (.+)$", result.stdout)
    if match is None or Path(match.group(1).strip()) != expected_program:
        raise UninstallError(f"loaded legacy service ownership is ambiguous for {label}")
    return True


def _bootout(launchctl: Path, uid: int, label: str) -> None:
    service = f"gui/{uid}/{label}"
    result = _run([str(launchctl), "bootout", service])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise UninstallError(f"could not unregister owned legacy service {label}: {detail[:300]}")


def _bootstrap_legacy(
    launchctl: Path, uid: int, path: Path, label: str, expected_program: Path
) -> None:
    result = _run([str(launchctl), "bootstrap", f"gui/{uid}", str(path)])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise UninstallError(f"could not restore owned legacy service {label}: {detail[:300]}")
    if not _legacy_loaded(launchctl, uid, label, expected_program):
        raise UninstallError(f"restored legacy service did not converge for {label}")


def _restore_modern_registration(app: Path, before: dict[str, str]) -> None:
    current = _service_snapshot(app, "status")
    operations = {
        "main_app": "register-main",
        "runtime_agent": "register-runtime",
    }
    for key in ("main_app", "runtime_agent"):
        if before[key] in REGISTERED_SERVICE_STATES and current[key] in ABSENT_SERVICE_STATES:
            current = _service_snapshot(app, operations[key])
    verified = _service_snapshot(app, "status")
    for key in ("main_app", "runtime_agent"):
        if before[key] in REGISTERED_SERVICE_STATES:
            if verified[key] not in REGISTERED_SERVICE_STATES:
                raise UninstallError(f"modern ServiceManagement compensation did not restore {key}")
        elif verified[key] not in ABSENT_SERVICE_STATES:
            raise UninstallError(f"modern ServiceManagement compensation invented registration for {key}")


def _preflight_transient(
    path: Path, *, directory: bool = False, require_empty: bool = False
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise UninstallError(f"owned transient state has unexpected type: {path}")
    if directory:
        if not path.is_dir():
            raise UninstallError(f"owned transient state has unexpected type: {path}")
        if require_empty:
            try:
                if any(path.iterdir()):
                    raise UninstallError("owned lifecycle lock is not empty; refusing broad cleanup")
            except OSError as exc:
                raise UninstallError("owned lifecycle lock cannot be inspected safely") from exc
        parent = path.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            raise UninstallError("owned lifecycle lock is not removable")
        return
    if not path.is_file():
        raise UninstallError(f"owned transient state has unexpected type: {path}")



def _remove_payload_store(payloads: Path) -> None:
    if not payloads.exists() and not payloads.is_symlink():
        return
    if payloads.is_symlink() or not payloads.is_dir():
        raise UninstallError("payload store ownership is ambiguous")
    for release in payloads.iterdir():
        package = release / "agent_runtime"
        if package.exists() and not package.is_symlink() and package.is_dir():
            package.chmod(0o700)
        release.chmod(0o700)
    payloads.chmod(0o700)
    shutil.rmtree(payloads)

def uninstall_product(*, home: Path, launchctl: Path, uid: int | None = None) -> dict[str, object]:
    home = home.expanduser()
    if not home.is_absolute():
        raise UninstallError("home must be an absolute path")
    uid = os.getuid() if uid is None else uid
    app = home / "Applications" / "Agent Runtime.app"
    state_dir = home / "Library" / "Application Support" / "Agent Runtime"
    transaction = state_dir / "cutover-transaction"
    runtime_env = state_dir / "runtime.env"
    if transaction.exists() or transaction.is_symlink():
        raise UninstallError("uninstall refused while a lifecycle transaction is pending")

    app_present = app.exists() or app.is_symlink()
    if app.is_symlink():
        raise UninstallError("installed app ownership is ambiguous")
    lifecycle_contract = _validate_app(app) if app_present else None

    launch_dir = home / "Library" / "LaunchAgents"
    current_plist = launch_dir / f"{MODERN_RUNTIME_LABEL}.plist"
    current_helper: Path | None = None
    current_loaded = False
    pointer: Path | None = None
    payloads: Path | None = None
    current_service_before: dict[str, str] | None = None
    desired_state = state_dir / "protected-runtime-running"
    desired_before = desired_state.exists() and not desired_state.is_symlink()

    if lifecycle_contract == "launchagent-v1":
        if not current_plist.exists() and not current_plist.is_symlink():
            predecessor_paths = (
                launch_dir / f"{UI_LABEL}.plist",
                launch_dir / f"{LEGACY_RUNTIME_LABEL}.plist",
            )
            if any(path.exists() or path.is_symlink() for path in predecessor_paths):
                lifecycle_contract = "legacy-launchagent-predecessor"
            else:
                raise UninstallError("current LaunchAgent ownership is missing")
        else:
            current_helper = _current_launchagent(current_plist, app, uid=uid)
            current_loaded = _legacy_loaded(
                launchctl, uid, MODERN_RUNTIME_LABEL, current_helper
            )
            pointer, payloads = _validate_payload_state(state_dir, uid=uid)
            current_service_before = _service_snapshot(app, "status")
            if current_service_before["runtime_agent"] not in ABSENT_SERVICE_STATES:
                raise UninstallError(
                    "current Runtime ServiceManagement ownership contradicts launchagent-v1"
                )
    elif current_plist.exists() or current_plist.is_symlink():
        raise UninstallError("current LaunchAgent exists outside its owned product contract")

    legacy: list[tuple[Path, str, Path, bool]] = []
    for label in (UI_LABEL, LEGACY_RUNTIME_LABEL):
        legacy_path = launch_dir / f"{label}.plist"
        if not legacy_path.exists() and not legacy_path.is_symlink():
            continue
        expected_program = _legacy_program(legacy_path, label, app)
        loaded = _legacy_loaded(launchctl, uid, label, expected_program)
        legacy.append((legacy_path, label, expected_program, loaded))

    if not app_present and not legacy:
        raise UninstallError("no proven Agent Runtime-owned product state is installed")

    transient_files = [
        desired_state,
        state_dir / "protected-attempts.json",
        state_dir / "menu-bar.lock",
    ]
    lifecycle_lock = state_dir / "lifecycle.lock"
    for transient in transient_files:
        _preflight_transient(transient)
    _preflight_transient(lifecycle_lock, directory=True, require_empty=True)

    predecessor_unregistered = False
    predecessor_before: dict[str, str] | None = None
    if lifecycle_contract == "servicemanagement-predecessor":
        predecessor_before = _service_snapshot(app, "status")
        if any(value in REGISTERED_SERVICE_STATES for value in predecessor_before.values()):
            after = _service_snapshot(app, "unregister")
            if any(value not in ABSENT_SERVICE_STATES for value in after.values()):
                raise UninstallError("predecessor services did not converge to an absent state")
            predecessor_unregistered = True
        elif any(value not in ABSENT_SERVICE_STATES for value in predecessor_before.values()):
            raise UninstallError("predecessor ServiceManagement ownership is ambiguous")

    booted_out: list[tuple[Path, str, Path]] = []
    current_stopped = False
    current_main_unregistered = False
    try:
        if lifecycle_contract == "launchagent-v1":
            _runtime_lifecycle(app, "stop")
            current_stopped = True
            if desired_state.exists() or desired_state.is_symlink():
                raise UninstallError("Runtime desired-state marker survived owned stop")
            if current_loaded:
                assert current_helper is not None
                _bootout(launchctl, uid, MODERN_RUNTIME_LABEL)
                booted_out.append((current_plist, MODERN_RUNTIME_LABEL, current_helper))

        for legacy_path, label, expected_program, loaded in legacy:
            if loaded:
                _bootout(launchctl, uid, label)
                booted_out.append((legacy_path, label, expected_program))

        if lifecycle_contract == "launchagent-v1" and current_service_before is not None:
            if current_service_before["main_app"] in REGISTERED_SERVICE_STATES:
                after = _service_snapshot(app, "unregister-main")
                if after["main_app"] not in ABSENT_SERVICE_STATES:
                    raise UninstallError("current main-app registration did not unregister")
                if after["runtime_agent"] not in ABSENT_SERVICE_STATES:
                    raise UninstallError(
                        "current Runtime ServiceManagement ownership changed during main-app unregister"
                    )
                current_main_unregistered = True
    except UninstallError as exc:
        compensation_errors: list[str] = []
        for restore_path, label, expected_program in reversed(booted_out):
            try:
                _bootstrap_legacy(
                    launchctl, uid, restore_path, label, expected_program
                )
            except UninstallError as compensation_error:
                compensation_errors.append(str(compensation_error))
        if current_stopped and lifecycle_contract == "launchagent-v1" and desired_before:
            try:
                _runtime_lifecycle(app, "start")
            except UninstallError as compensation_error:
                compensation_errors.append(str(compensation_error))
        if current_main_unregistered and current_service_before is not None:
            try:
                _restore_modern_registration(app, current_service_before)
            except UninstallError as compensation_error:
                compensation_errors.append(str(compensation_error))
        if predecessor_unregistered and predecessor_before is not None:
            try:
                _restore_modern_registration(app, predecessor_before)
            except UninstallError as compensation_error:
                compensation_errors.append(str(compensation_error))
        if compensation_errors:
            raise UninstallError(
                str(exc) + "; uninstall compensation failed: " + "; ".join(compensation_errors)
            ) from exc
        raise

    if lifecycle_contract == "launchagent-v1":
        current_plist.unlink()
        assert pointer is not None and payloads is not None
        pointer.unlink()
        _remove_payload_store(payloads)

    for legacy_path, _, _, _ in legacy:
        legacy_path.unlink()

    for transient in transient_files:
        if transient.exists() or transient.is_symlink():
            transient.unlink()
    if lifecycle_lock.exists() or lifecycle_lock.is_symlink():
        if lifecycle_lock.is_symlink():
            lifecycle_lock.unlink()
        else:
            try:
                lifecycle_lock.rmdir()
            except OSError as exc:
                raise UninstallError("owned lifecycle lock is not empty; refusing broad cleanup") from exc

    if app_present:
        shutil.rmtree(app)

    return {
        "status": "UNINSTALLED",
        "lifecycle_contract": lifecycle_contract,
        "legacy_remnants_removed": len(legacy),
        "payload_state_removed": lifecycle_contract == "launchagent-v1",
        "main_app_registration_removed": current_main_unregistered,
        "retained_configuration": str(runtime_env) if runtime_env.exists() else None,
        "configuration_removed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Uninstall proven Agent Runtime-owned per-user state.")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--launchctl", type=Path, default=Path("/bin/launchctl"))
    args = parser.parse_args()
    try:
        result = uninstall_product(home=args.home, launchctl=args.launchctl)
    except UninstallError as exc:
        print("UNINSTALL ERROR: " + str(exc), file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
