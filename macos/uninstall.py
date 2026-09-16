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
RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
SERVICE_PLIST = f"{RUNTIME_LABEL}.plist"
KNOWN_SERVICE_STATES = {"enabled", "requires-approval", "not-registered", "not-found"}


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


def _validate_app(app: Path) -> bool:
    info = _load_plist(app / "Contents" / "Info.plist")
    if info.get("CFBundleIdentifier") != OWNER or info.get("CFBundleExecutable") != "AgentRuntimeMenuBar":
        raise UninstallError("installed app ownership is ambiguous")
    main = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
    runtime_start = app / "Contents" / "Resources" / "runtime" / "start.sh"
    if main.is_symlink() or not main.is_file() or runtime_start.is_symlink() or not runtime_start.is_file():
        raise UninstallError("installed app ownership is incomplete")
    service_plist = app / "Contents" / "Library" / "LaunchAgents" / SERVICE_PLIST
    if not service_plist.exists() and not service_plist.is_symlink():
        return False
    service = _load_plist(service_plist)
    helper = app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
    if (
        service.get("Label") != RUNTIME_LABEL
        or service.get("BundleProgram") != "Contents/MacOS/AgentRuntimeRuntimeService"
        or helper.is_symlink()
        or not helper.is_file()
    ):
        raise UninstallError("modern service ownership is ambiguous")
    return True


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


def _restore_modern_registration(app: Path) -> None:
    restored = _service_snapshot(app, "register")
    if any(value not in {"enabled", "requires-approval"} for value in restored.values()):
        raise UninstallError("modern ServiceManagement compensation did not converge")


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
        raise UninstallError("uninstall refused while a cutover transaction is pending")

    app_present = app.exists() or app.is_symlink()
    if app.is_symlink():
        raise UninstallError("installed app ownership is ambiguous")
    modern = _validate_app(app) if app_present else False

    launch_dir = home / "Library" / "LaunchAgents"
    legacy: list[tuple[Path, str, Path, bool]] = []
    for label in (UI_LABEL, RUNTIME_LABEL):
        path = launch_dir / f"{label}.plist"
        if not path.exists() and not path.is_symlink():
            continue
        expected_program = _legacy_program(path, label, app)
        loaded = _legacy_loaded(launchctl, uid, label, expected_program)
        legacy.append((path, label, expected_program, loaded))

    if not app_present and not legacy:
        raise UninstallError("no proven Agent Runtime-owned product state is installed")

    transient_files = [
        state_dir / "protected-runtime-running",
        state_dir / "protected-attempts.json",
        state_dir / "menu-bar.lock",
    ]
    lifecycle_lock = state_dir / "lifecycle.lock"
    for path in transient_files:
        _preflight_transient(path)
    _preflight_transient(lifecycle_lock, directory=True, require_empty=True)

    modern_unregistered = False
    if modern:
        before = _service_snapshot(app, "status")
        if "not-found" in before.values():
            raise UninstallError("modern ServiceManagement ownership is ambiguous")
        after = _service_snapshot(app, "unregister")
        if after != {"main_app": "not-registered", "runtime_agent": "not-registered"}:
            raise UninstallError("modern services did not converge to not-registered")
        modern_unregistered = True

    booted_out: list[tuple[Path, str, Path]] = []
    try:
        for path, label, expected_program, loaded in legacy:
            if loaded:
                _bootout(launchctl, uid, label)
                booted_out.append((path, label, expected_program))
    except UninstallError as exc:
        compensation_errors: list[str] = []
        for path, label, expected_program in reversed(booted_out):
            try:
                _bootstrap_legacy(launchctl, uid, path, label, expected_program)
            except UninstallError as compensation_error:
                compensation_errors.append(str(compensation_error))
        if modern_unregistered:
            try:
                _restore_modern_registration(app)
            except UninstallError as compensation_error:
                compensation_errors.append(str(compensation_error))
        if compensation_errors:
            raise UninstallError(
                str(exc) + "; uninstall compensation failed: " + "; ".join(compensation_errors)
            ) from exc
        raise

    for path, _, _, _ in legacy:
        path.unlink()

    for path in transient_files:
        if path.exists() or path.is_symlink():
            path.unlink()
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
        "legacy_remnants_removed": len(legacy),
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
