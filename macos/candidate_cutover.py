#!/usr/bin/env python3
"""Transactional installation of an already-sealed Agent Runtime.app candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MACOS_ROOT = Path(__file__).resolve().parent
if str(MACOS_ROOT) not in sys.path:
    sys.path.insert(0, str(MACOS_ROOT))
import package_provenance as provenance

TRANSACTION_SCHEMA = 2
LEGACY_RECOVERY_SCHEMA = 1
UI_LABEL = "com.picmao.agent-runtime-ui"
RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
APP_BUNDLE_IDENTIFIER = "com.picmao.agent-runtime"
RUNTIME_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SERVICE_ABSENCE_TIMEOUT_SECONDS = 5.0
SERVICE_POLL_INTERVAL_SECONDS = 0.05
LAUNCHCTL_DIAGNOSTIC_LIMIT = 1536


class CutoverError(RuntimeError):
    pass


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


SERVICE_STATES = {"enabled", "requires-approval", "not-registered", "not-found"}
REGISTERED_SERVICE_STATES = {"enabled", "requires-approval"}
ABSENT_SERVICE_STATES = {"not-registered", "not-found"}


def _service_management(app: Path, operation: str) -> dict[str, str]:
    executable = app / "Contents" / "MacOS" / "AgentRuntimeMenuBar"
    if executable.is_symlink() or not executable.is_file():
        raise CutoverError("candidate ServiceManagement executable is missing or unsafe")
    result = _run([str(executable), "--service-management", operation])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CutoverError(f"ServiceManagement {operation} failed: {detail[:300]}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CutoverError("ServiceManagement diagnostics are malformed") from exc
    if not isinstance(value, dict) or set(value) != {"main_app", "runtime_agent"}:
        raise CutoverError("ServiceManagement diagnostics are malformed")
    if any(not isinstance(value[key], str) or value[key] not in SERVICE_STATES for key in value):
        raise CutoverError("ServiceManagement diagnostics contain an unknown state")
    return value


def _bounded_launchctl_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*(?:=>|=|:)\s*[^\r\n]*",
        r"\1=<redacted>",
        value,
    )
    if len(redacted) <= LAUNCHCTL_DIAGNOSTIC_LIMIT:
        return redacted
    return redacted[:LAUNCHCTL_DIAGNOSTIC_LIMIT] + "...<truncated>"


def _require_launchctl_ok(result: subprocess.CompletedProcess[str], message: str) -> None:
    if result.returncode == 0:
        return
    args = result.args if isinstance(result.args, (list, tuple)) else []
    operation = str(args[1]) if len(args) > 1 else "unknown"
    stdout = _bounded_launchctl_text(result.stdout or "")
    stderr = _bounded_launchctl_text(result.stderr or "")
    raise CutoverError(
        f"{message}; launchctl operation={operation} returncode={result.returncode} "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


def _service_print(launchctl: Path, service: str) -> subprocess.CompletedProcess[str] | None:
    result = _run([str(launchctl), "print", service])
    if result.returncode == 0:
        return result
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode == 113 and "Could not find service" in combined:
        return None
    _require_launchctl_ok(result, f"could not inspect LaunchAgent state: {service}")
    raise AssertionError("unreachable")


def _service_loaded(launchctl: Path, service: str) -> bool:
    return _service_print(launchctl, service) is not None


def _loaded_service_program(launchctl: Path, service: str) -> Path | None:
    result = _service_print(launchctl, service)
    if result is None:
        return None
    programs = []
    for raw in result.stdout.splitlines():
        if raw.startswith("\tprogram = "):
            programs.append(raw[len("\tprogram = "):].strip())
    if len(programs) != 1 or not programs[0]:
        raise CutoverError(f"LaunchAgent identity mismatch: program is unavailable: {service}")
    return Path(programs[0])


def _sha256_file(path: Path, description: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise CutoverError(f"{description} is missing or unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_bundle_identity(app: Path) -> dict[str, str]:
    helper = app / "Contents" / "MacOS" / "AgentRuntimeRuntimeService"
    plist = app / "Contents" / "Library" / "LaunchAgents" / f"{RUNTIME_LABEL}.plist"
    return {
        "helper_program": str(helper),
        "helper_sha256": _sha256_file(helper, "Runtime helper"),
        "plist_sha256": _sha256_file(plist, "Runtime LaunchAgent plist"),
    }


def _classify_runtime_ownership(
    registration_state: str,
    loaded_program: Path | None,
    expected_program: Path,
) -> str:
    if registration_state not in SERVICE_STATES:
        raise CutoverError("Runtime ServiceManagement state is invalid")
    if loaded_program is not None and loaded_program != expected_program:
        raise CutoverError("Runtime LaunchAgent program identity does not match the installed helper")
    if registration_state == "enabled":
        return "healthy-registered" if loaded_program is not None else "stale-registered"
    if registration_state == "requires-approval":
        return "awaiting-approval"
    if loaded_program is not None:
        raise CutoverError("Runtime LaunchAgent is loaded without registered ServiceManagement ownership")
    return "absent"


def _empty_operation_ledger() -> dict[str, bool]:
    return {
        "main_registered": False,
        "main_unregistered": False,
        "runtime_unregistered": False,
        "runtime_registered": False,
    }


def _validate_operation_ledger(value: object) -> dict[str, bool]:
    expected = set(_empty_operation_ledger())
    if not isinstance(value, dict) or set(value) != expected:
        raise CutoverError("cutover operation ledger is malformed")
    if any(not isinstance(value[key], bool) for key in expected):
        raise CutoverError("cutover operation ledger is malformed")
    return {key: bool(value[key]) for key in _empty_operation_ledger()}


def _wait_for_service_absence(
    launchctl: Path,
    service: str,
    *,
    timeout_seconds: float = SERVICE_ABSENCE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = SERVICE_POLL_INTERVAL_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _service_loaded(launchctl, service):
        if time.monotonic() >= deadline:
            raise CutoverError(f"timed out waiting for LaunchAgent absence: {service}")
        time.sleep(poll_interval_seconds)


def _require_service_identity(launchctl: Path, service: str, expected_program: Path) -> None:
    result = _service_print(launchctl, service)
    if result is None:
        raise CutoverError(f"LaunchAgent identity mismatch: service is absent: {service}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines or lines[0].strip() != f"{service} = {{":
        raise CutoverError(f"LaunchAgent identity mismatch: label does not match {service}")
    top_level_indented = any(raw.startswith("\tprogram = ") for raw in lines)
    programs: list[str] = []
    for raw in lines:
        if top_level_indented and (not raw.startswith("\t") or raw.startswith("\t\t")):
            continue
        line = raw.strip()
        if line.startswith("program = "):
            programs.append(line[len("program = "):].strip())
    if programs != [str(expected_program)]:
        raise CutoverError(f"LaunchAgent identity mismatch: program does not match {service}")


def _launchagent_program(plist: Path, expected_label: str) -> Path:
    try:
        payload = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise CutoverError(f"LaunchAgent plist is malformed: {plist}") from exc
    arguments = payload.get("ProgramArguments")
    if payload.get("Label") != expected_label or not isinstance(arguments, list) or not arguments:
        raise CutoverError(f"LaunchAgent plist identity is invalid: {plist}")
    return Path(str(arguments[0]))


def _runtime_bundle_present(app: Path) -> bool:
    return (
        (app / "Contents/MacOS/AgentRuntimeMenuBar").is_file()
        and (app / "Contents/MacOS/AgentRuntimeRuntimeService").is_file()
        and (app / "Contents/Library/LaunchAgents" / f"{RUNTIME_LABEL}.plist").is_file()
    )


def _modern_ownership_snapshot(
    target_app: Path,
    *,
    launchctl: Path,
    uid: int,
    runtime_legacy_program: Path | None,
) -> dict[str, object]:
    if target_app.exists() and _runtime_bundle_present(target_app):
        service_state = _service_management(target_app, "status")
        identity = _runtime_bundle_identity(target_app)
        expected_program = Path(identity["helper_program"])
    else:
        service_state = {"main_app": "not-found", "runtime_agent": "not-found"}
        identity = {"helper_program": "", "helper_sha256": "", "plist_sha256": ""}
        expected_program = target_app / "Contents/MacOS/AgentRuntimeRuntimeService"

    runtime_service = f"gui/{uid}/{RUNTIME_LABEL}"
    loaded_program = _loaded_service_program(launchctl, runtime_service)
    modern_loaded_program = loaded_program
    legacy_loaded = False
    if loaded_program is not None and runtime_legacy_program is not None and loaded_program == runtime_legacy_program:
        legacy_loaded = True
        modern_loaded_program = None
    classification = _classify_runtime_ownership(
        service_state["runtime_agent"], modern_loaded_program, expected_program
    )
    return {
        "main_app": service_state["main_app"],
        "runtime": {
            "registration_state": service_state["runtime_agent"],
            "classification": classification,
            "loaded": modern_loaded_program is not None,
            "loaded_program": str(modern_loaded_program) if modern_loaded_program is not None else "",
            "legacy_label_loaded": legacy_loaded,
            "helper_program": identity["helper_program"],
            "helper_sha256": identity["helper_sha256"],
            "plist_sha256": identity["plist_sha256"],
        },
    }


def _runtime_generation_changed(before: dict[str, object], candidate_app: Path) -> bool:
    candidate = _runtime_bundle_identity(candidate_app)
    runtime = before.get("runtime")
    if not isinstance(runtime, dict):
        raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    return (
        runtime.get("helper_sha256") != candidate["helper_sha256"]
        or runtime.get("plist_sha256") != candidate["plist_sha256"]
    )


def _validate_runtime_ownership_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    required = {
        "registration_state", "classification", "loaded", "loaded_program", "legacy_label_loaded",
        "helper_program", "helper_sha256", "plist_sha256",
    }
    if set(value) != required:
        raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    if value["registration_state"] not in SERVICE_STATES:
        raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    if value["classification"] not in {"healthy-registered", "stale-registered", "absent", "awaiting-approval"}:
        raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    if not isinstance(value["loaded"], bool) or not isinstance(value["legacy_label_loaded"], bool):
        raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    for key in ("loaded_program", "helper_program", "helper_sha256", "plist_sha256"):
        if not isinstance(value[key], str):
            raise CutoverError("pre-swap Runtime ownership metadata is malformed")
    return dict(value)



def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)


def _load_metadata(
    transaction_dir: Path,
    *,
    allowed_schemas: set[int] | None = None,
) -> dict[str, object]:
    path = transaction_dir / "metadata.json"
    if path.is_symlink() or not path.is_file():
        raise CutoverError("cutover transaction metadata is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CutoverError("cutover transaction metadata is malformed") from exc
    schemas = {TRANSACTION_SCHEMA} if allowed_schemas is None else set(allowed_schemas)
    if not isinstance(value, dict) or value.get("schema") not in schemas:
        raise CutoverError("cutover transaction metadata schema is invalid")
    return value


def _copy_app(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise CutoverError("app bundle must be a regular non-symlink directory")
    if destination.exists() or destination.is_symlink():
        raise CutoverError("app copy destination must not already exist")
    shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=False)


def _rollback_app_closure(app: Path) -> dict[str, object]:
    if app.is_symlink() or not app.is_dir():
        raise CutoverError("rollback app root must be a regular non-symlink directory")
    records: list[tuple[bytes, dict[str, object]]] = []
    paths = [app]
    for current, dirs, files in os.walk(app, topdown=True, followlinks=False):
        root = Path(current)
        paths.extend(root / name for name in dirs)
        paths.extend(root / name for name in files)
    for candidate in paths:
        info = candidate.lstat()
        relative = "." if candidate == app else candidate.relative_to(app).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            record: dict[str, object] = {"path": relative, "type": "directory", "mode": mode}
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            record = {
                "path": relative,
                "type": "file",
                "mode": mode,
                "size": info.st_size,
                "sha256": digest.hexdigest(),
            }
        elif stat.S_ISLNK(info.st_mode):
            record = {"path": relative, "type": "symlink", "target": os.readlink(candidate)}
        else:
            raise CutoverError(f"rollback app contains unsupported entry: {candidate}")
        records.append((os.fsencode(relative), record))
    records.sort(key=lambda item: item[0])
    canonical = json.dumps(
        [record for _, record in records],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {"record_count": len(records), "sha256": hashlib.sha256(canonical).hexdigest()}


def _require_rollback_app_closure(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"record_count", "sha256"}:
        raise CutoverError("rollback app closure metadata is invalid")
    count = value.get("record_count")
    digest = value.get("sha256")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise CutoverError("rollback app closure metadata is invalid")
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise CutoverError("rollback app closure metadata is invalid")
    return {"record_count": count, "sha256": digest}


def _copy_rollback_app(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise CutoverError("rollback app source must be a regular non-symlink directory")
    if destination.exists() or destination.is_symlink():
        raise CutoverError("rollback app copy destination must not already exist")
    shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=True)


def _validate_previous_launchagent(path: Path, label: str, target_app: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    kind = "Runtime" if label == RUNTIME_LABEL else "UI"
    if path.is_symlink() or not path.is_file():
        raise CutoverError(f"previous {kind} LaunchAgent is not a regular non-symlink file")
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise CutoverError(f"previous {kind} LaunchAgent is malformed") from exc
    if payload.get("Label") != label:
        raise CutoverError(f"previous {kind} LaunchAgent ownership is invalid")
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list):
        raise CutoverError(f"previous {kind} LaunchAgent arguments are invalid")
    if label == UI_LABEL:
        expected = [str(target_app / "Contents/MacOS/AgentRuntimeMenuBar")]
        if arguments != expected:
            raise CutoverError("previous UI LaunchAgent does not point to the installed app")
    else:
        expected_prefix = [str(target_app / "Contents/Resources/runtime/start.sh"), "--serve"]
        if len(arguments) != 3 or arguments[:2] != expected_prefix or not Path(str(arguments[2])).is_absolute():
            raise CutoverError("previous Runtime LaunchAgent does not point to the installed app")
        environment = payload.get("EnvironmentVariables", {})
        if not isinstance(environment, dict) or "RUNTIME_ENV_FILE" in environment:
            raise CutoverError("previous Runtime LaunchAgent configuration ownership is invalid")
    return True


def _snapshot_file(path: Path, destination: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"present": False, "mode": None}
    if path.is_symlink() or not path.is_file():
        raise CutoverError(f"rollback source is not a regular file: {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return {"present": True, "mode": path.stat().st_mode & 0o7777}


def _restore_file(snapshot: dict[str, object], backup: Path, target: Path) -> None:
    if target.is_symlink():
        raise CutoverError(f"refusing to replace symlinked rollback target: {target}")
    if snapshot["present"]:
        if not backup.is_file() or backup.is_symlink():
            raise CutoverError(f"rollback backup is missing or unsafe: {backup}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".rollback")
        shutil.copy2(backup, temp)
        os.replace(temp, target)
        target.chmod(int(snapshot["mode"]))
    elif target.exists():
        if not target.is_file():
            raise CutoverError(f"rollback target is not a regular file: {target}")
        target.unlink()


def _inject(fail_stages: set[str], stage: str) -> None:
    if stage in fail_stages:
        raise CutoverError(f"injected {stage} failure")



def _remove_legacy_predecessor(
    *,
    launchctl: Path,
    uid: int,
    ui_plist: Path,
    runtime_plist: Path,
    ui_present: bool,
    runtime_present: bool,
    ui_was_loaded: bool,
    runtime_was_loaded: bool,
    preserve_modern_runtime: bool = False,
    modern_runtime_program: Path | None = None,
) -> None:
    domain = f"gui/{uid}"
    ui_service = f"{domain}/{UI_LABEL}"
    runtime_service = f"{domain}/{RUNTIME_LABEL}"

    ui_loaded_now = _service_loaded(launchctl, ui_service)
    if ui_loaded_now != ui_was_loaded:
        raise CutoverError(f"legacy LaunchAgent state changed during cutover: {ui_service}")
    if ui_was_loaded:
        _require_launchctl_ok(
            _run([str(launchctl), "bootout", ui_service]),
            f"could not unregister legacy LaunchAgent: {ui_service}",
        )
        _wait_for_service_absence(launchctl, ui_service)

    runtime_program = _loaded_service_program(launchctl, runtime_service)
    if runtime_was_loaded:
        if runtime_program is None:
            raise CutoverError(f"legacy LaunchAgent state changed during cutover: {runtime_service}")
        _require_launchctl_ok(
            _run([str(launchctl), "bootout", runtime_service]),
            f"could not unregister legacy LaunchAgent: {runtime_service}",
        )
        _wait_for_service_absence(launchctl, runtime_service)
    elif runtime_program is not None:
        if not preserve_modern_runtime or modern_runtime_program is None or runtime_program != modern_runtime_program:
            raise CutoverError(f"Runtime LaunchAgent ownership changed during cutover: {runtime_service}")

    for plist, present in ((ui_plist, ui_present), (runtime_plist, runtime_present)):
        if present:
            if plist.is_symlink() or not plist.is_file():
                raise CutoverError(f"legacy LaunchAgent ownership changed during cutover: {plist}")
            plist.unlink()
        if plist.exists() or plist.is_symlink():
            raise CutoverError(f"legacy LaunchAgent file remained after migration: {plist}")

    if _service_loaded(launchctl, ui_service):
        raise CutoverError(f"legacy LaunchAgent remained registered after migration: {ui_service}")
    runtime_after = _loaded_service_program(launchctl, runtime_service)
    if preserve_modern_runtime:
        if modern_runtime_program is None or runtime_after != modern_runtime_program:
            raise CutoverError("preserved modern Runtime LaunchAgent identity changed during migration")
    elif runtime_after is not None:
        raise CutoverError(f"legacy LaunchAgent remained registered after migration: {runtime_service}")


def _restore_loaded_state(
    *,
    launchctl: Path,
    uid: int,
    ui_plist: Path,
    runtime_plist: Path,
    ui_loaded: bool,
    runtime_loaded: bool,
) -> None:
    domain = f"gui/{uid}"
    pairs = (
        (f"{domain}/{UI_LABEL}", ui_plist, ui_loaded),
        (f"{domain}/{RUNTIME_LABEL}", runtime_plist, runtime_loaded),
    )
    for service, plist, expected_loaded in pairs:
        if _service_loaded(launchctl, service):
            _require_launchctl_ok(
                _run([str(launchctl), "bootout", service]),
                f"could not unload service during rollback: {service}",
            )
            _wait_for_service_absence(launchctl, service)
        if expected_loaded:
            _require_launchctl_ok(
                _run([str(launchctl), "bootstrap", domain, str(plist)]),
                f"could not restore service during rollback: {service}",
            )
            label = service.rsplit("/", 1)[-1]
            _require_service_identity(launchctl, service, _launchagent_program(plist, label))
        if _service_loaded(launchctl, service) != expected_loaded:
            raise CutoverError(f"restored LaunchAgent loaded state mismatch: {service}")


def _validate_recorded_service_state(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"main_app", "runtime_agent"}:
        raise CutoverError("recorded ServiceManagement state is malformed")
    if any(not isinstance(value[key], str) or value[key] not in SERVICE_STATES for key in value):
        raise CutoverError("recorded ServiceManagement state is malformed")
    return {"main_app": value["main_app"], "runtime_agent": value["runtime_agent"]}


def _compensate_operation_ledger(target_app: Path, ledger_value: object) -> None:
    ledger = _validate_operation_ledger(ledger_value)
    state = _service_management(target_app, "status")
    if ledger["runtime_registered"] and state["runtime_agent"] in REGISTERED_SERVICE_STATES:
        state = _service_management(target_app, "unregister-runtime")
    if ledger["main_registered"] and state["main_app"] in REGISTERED_SERVICE_STATES:
        state = _service_management(target_app, "unregister-main")
    verified = _service_management(target_app, "status")
    if ledger["runtime_registered"] and verified["runtime_agent"] not in ABSENT_SERVICE_STATES:
        raise CutoverError("transaction-created Runtime ServiceManagement state remained active")
    if ledger["main_registered"] and verified["main_app"] not in ABSENT_SERVICE_STATES:
        raise CutoverError("transaction-created main-app ServiceManagement state remained active")


def _restore_preexisting_modern_state(
    target_app: Path,
    metadata: dict[str, object],
    *,
    launchctl: Path,
    uid: int,
) -> None:
    before = metadata.get("modern_ownership_before")
    if not isinstance(before, dict) or set(before) != {"main_app", "runtime"}:
        raise CutoverError("pre-swap ServiceManagement ownership metadata is malformed")
    runtime = _validate_runtime_ownership_record(before["runtime"])
    ledger = _validate_operation_ledger(metadata.get("operations"))

    if ledger["main_unregistered"] and before["main_app"] in REGISTERED_SERVICE_STATES:
        _service_management(target_app, "register-main")

    if ledger["runtime_unregistered"] and runtime["classification"] in {"healthy-registered", "awaiting-approval"}:
        restored = _service_management(target_app, "register-runtime")
        if restored["runtime_agent"] not in REGISTERED_SERVICE_STATES:
            raise CutoverError("pre-existing Runtime ServiceManagement state could not be restored")
        if runtime["classification"] == "healthy-registered":
            expected = target_app / "Contents/MacOS/AgentRuntimeRuntimeService"
            _require_service_identity(launchctl, f"gui/{uid}/{RUNTIME_LABEL}", expected)


def _unregister_modern_generation(
    transaction_dir: Path,
    target_app: Path,
    *,
    registration_before: dict[str, str] | None = None,
) -> None:
    if not target_app.exists() and not target_app.is_symlink():
        return
    if target_app.is_symlink() or not target_app.is_dir():
        raise CutoverError("installed app path is unsafe before modern rollback")
    provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")
    if registration_before is None:
        metadata = _load_metadata(transaction_dir)
        recorded = metadata.get("modern_registration_before")
        if recorded is None:
            return
        registration_before = _validate_recorded_service_state(recorded)
    else:
        registration_before = _validate_recorded_service_state(registration_before)

    state = _service_management(target_app, "status")
    created = {
        key: registration_before[key] in ABSENT_SERVICE_STATES and state[key] in REGISTERED_SERVICE_STATES
        for key in ("main_app", "runtime_agent")
    }
    for key in ("main_app", "runtime_agent"):
        if registration_before[key] in REGISTERED_SERVICE_STATES and state[key] not in REGISTERED_SERVICE_STATES:
            raise CutoverError(f"pre-existing modern ServiceManagement state changed during rollback: {key}")

    if created["runtime_agent"]:
        _service_management(target_app, "unregister-runtime")
    if created["main_app"]:
        _service_management(target_app, "unregister-main")

    verified = _service_management(target_app, "status")
    for key in ("main_app", "runtime_agent"):
        before = registration_before[key]
        if before in REGISTERED_SERVICE_STATES:
            if verified[key] != before:
                raise CutoverError(f"pre-existing modern ServiceManagement state was not preserved: {key}")
        elif verified[key] not in ABSENT_SERVICE_STATES:
            raise CutoverError(f"transaction-created modern ServiceManagement state remained active: {key}")


def _restore_transaction(
    transaction_dir: Path,
    target_app: Path,
    *,
    launchctl: Path,
    uid: int,
    fail_stages: set[str],
) -> None:
    metadata = _load_metadata(transaction_dir)
    previous = metadata.get("previous")
    if not isinstance(previous, dict):
        raise CutoverError("cutover transaction previous-state metadata is invalid")
    app_present = previous.get("app_present")
    if not isinstance(app_present, bool):
        raise CutoverError("rollback previous-app presence metadata is invalid")
    if target_app.is_symlink() or not target_app.is_dir():
        raise CutoverError("installed app path is unsafe before rollback")
    provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")

    expected_closure: dict[str, object] | None = None
    backup = transaction_dir / "previous-app"
    if app_present:
        expected_closure = _require_rollback_app_closure(previous.get("app_closure"))
        if _rollback_app_closure(backup) != expected_closure:
            raise CutoverError("rollback app snapshot closure does not match rollback envelope")

    _compensate_operation_ledger(target_app, metadata.get("operations"))
    _inject(fail_stages, "rollback_restore_app")

    if target_app.exists():
        if not target_app.is_dir():
            raise CutoverError("installed app path is not a directory during rollback")
        shutil.rmtree(target_app)
    if app_present:
        _copy_rollback_app(backup, target_app)
        if expected_closure is None or _rollback_app_closure(target_app) != expected_closure:
            raise CutoverError("restored previous app closure does not match rollback envelope")

    paths = metadata.get("paths")
    if not isinstance(paths, dict):
        raise CutoverError("cutover transaction path metadata is invalid")
    ui_plist = Path(str(paths["ui_plist"]))
    runtime_plist = Path(str(paths["runtime_plist"]))
    desired_state = Path(str(paths["desired_state"]))
    _restore_file(previous["ui_plist"], transaction_dir / "previous-ui.plist", ui_plist)
    _restore_file(previous["runtime_plist"], transaction_dir / "previous-runtime.plist", runtime_plist)

    desired_before = bool(previous.get("desired_state_present"))
    if desired_before:
        desired_state.parent.mkdir(parents=True, exist_ok=True)
        desired_state.touch(exist_ok=True)
    elif desired_state.exists() or desired_state.is_symlink():
        if desired_state.is_symlink() or not desired_state.is_file():
            raise CutoverError("desired-state rollback target is unsafe")
        desired_state.unlink()

    runtime_was_loaded = bool(previous.get("runtime_loaded"))
    _restore_loaded_state(
        launchctl=launchctl,
        uid=uid,
        ui_plist=ui_plist,
        runtime_plist=runtime_plist,
        ui_loaded=bool(previous.get("ui_loaded")),
        runtime_loaded=runtime_was_loaded,
    )
    _restore_preexisting_modern_state(target_app, metadata, launchctl=launchctl, uid=uid)
    if runtime_was_loaded and desired_before:
        runtime_service = f"gui/{uid}/{RUNTIME_LABEL}"
        _require_launchctl_ok(
            _run([str(launchctl), "kickstart", "-k", runtime_service]),
            "could not refresh the restored Runtime LaunchAgent",
        )


def rollback_transaction(
    transaction_dir: Path,
    target_app: Path,
    *,
    launchctl: Path,
    uid: int,
    fail_stages: set[str] | None = None,
) -> dict[str, object]:
    failures = set(fail_stages or ())
    metadata = _load_metadata(transaction_dir)
    try:
        _restore_transaction(
            transaction_dir,
            target_app,
            launchctl=launchctl,
            uid=uid,
            fail_stages=failures,
        )
    except Exception as exc:
        metadata["status"] = "PARTIAL"
        metadata["last_error"] = "rollback incomplete: " + str(exc)
        _atomic_json(transaction_dir / "metadata.json", metadata)
        if isinstance(exc, CutoverError):
            raise
        raise CutoverError("rollback incomplete") from exc
    shutil.rmtree(transaction_dir)
    return {"status": "ROLLED_BACK"}


def cutover_candidate(
    candidate_app: Path,
    handoff_path: Path,
    *,
    target_app: Path,
    ui_plist: Path,
    runtime_plist: Path,
    state_dir: Path,
    transaction_dir: Path,
    home: Path,
    launchctl: Path,
    tunnel_client: Path,
    uid: int,
    fail_stages: set[str] | None = None,
) -> dict[str, object]:
    failures = set(fail_stages or ())
    if transaction_dir.exists() or transaction_dir.is_symlink():
        raise CutoverError("a cutover transaction is already pending")
    if candidate_app.resolve() == target_app.resolve(strict=False):
        raise CutoverError("prebuilt candidate must be external to the installed app path")
    expected = provenance.validate_candidate(candidate_app, handoff_path)

    domain = f"gui/{uid}"
    ui_service = f"{domain}/{UI_LABEL}"
    runtime_service = f"{domain}/{RUNTIME_LABEL}"
    desired_state = state_dir / "protected-runtime-running"
    if desired_state.is_symlink() or (desired_state.exists() and not desired_state.is_file()):
        raise CutoverError("desired Runtime state marker is unsafe")

    ui_present = _validate_previous_launchagent(ui_plist, UI_LABEL, target_app)
    runtime_present = _validate_previous_launchagent(runtime_plist, RUNTIME_LABEL, target_app)
    if (ui_present or runtime_present) and not target_app.exists():
        raise CutoverError("previous LaunchAgent state exists without an installed app rollback target")

    ui_loaded_program = _loaded_service_program(launchctl, ui_service)
    ui_legacy_program = _launchagent_program(ui_plist, UI_LABEL) if ui_present else None
    if ui_loaded_program is not None and ui_loaded_program != ui_legacy_program:
        raise CutoverError("legacy UI LaunchAgent loaded identity is ambiguous")
    ui_loaded = ui_loaded_program is not None

    runtime_legacy_program = _launchagent_program(runtime_plist, RUNTIME_LABEL) if runtime_present else None
    modern_before = _modern_ownership_snapshot(
        target_app, launchctl=launchctl, uid=uid, runtime_legacy_program=runtime_legacy_program
    )
    runtime_before = _validate_runtime_ownership_record(modern_before["runtime"])
    runtime_legacy_loaded = bool(runtime_before["legacy_label_loaded"])
    if runtime_legacy_loaded and not runtime_present:
        raise CutoverError("previous Runtime LaunchAgent is loaded but its plist is unavailable")
    if ui_loaded and not ui_present:
        raise CutoverError("previous UI LaunchAgent is loaded but its plist is unavailable")

    generation_changed = _runtime_generation_changed(modern_before, candidate_app)
    runtime_refresh = (
        runtime_before["registration_state"] in REGISTERED_SERVICE_STATES
        and (runtime_before["classification"] == "stale-registered" or generation_changed)
    )
    preserve_modern_runtime = runtime_before["classification"] == "healthy-registered" and not runtime_refresh

    previous: dict[str, object] = {
        "app_present": False,
        "app_closure": None,
        "ui_loaded": ui_loaded,
        "runtime_loaded": runtime_legacy_loaded,
        "desired_state_present": desired_state.exists(),
    }
    operations = _empty_operation_ledger()
    created_transaction = False
    mutation_started = False
    try:
        transaction_dir.mkdir(parents=True, mode=0o700)
        created_transaction = True
        previous["ui_plist"] = _snapshot_file(ui_plist, transaction_dir / "previous-ui.plist")
        previous["runtime_plist"] = _snapshot_file(runtime_plist, transaction_dir / "previous-runtime.plist")

        if target_app.exists() or target_app.is_symlink():
            if target_app.is_symlink() or not target_app.is_dir():
                raise CutoverError("existing installed app path is unsafe")
            previous_closure = _rollback_app_closure(target_app)
            _copy_rollback_app(target_app, transaction_dir / "previous-app")
            if _rollback_app_closure(transaction_dir / "previous-app") != previous_closure:
                raise CutoverError("previous app rollback copy changed closure")
            if _rollback_app_closure(target_app) != previous_closure:
                raise CutoverError("previous installed app changed while creating rollback snapshot")
            previous["app_present"] = True
            previous["app_closure"] = previous_closure

        shutil.copy2(handoff_path, transaction_dir / "candidate-handoff.json")
        staged = transaction_dir / "staged-candidate"
        _copy_app(candidate_app, staged)
        provenance.validate_candidate(staged, transaction_dir / "candidate-handoff.json")

        metadata: dict[str, object] = {
            "schema": TRANSACTION_SCHEMA,
            "status": "PREPARED",
            "candidate": expected,
            "previous": previous,
            "modern_ownership_before": modern_before,
            "runtime_generation_changed": generation_changed,
            "operations": operations,
            "paths": {
                "target_app": str(target_app),
                "ui_plist": str(ui_plist),
                "runtime_plist": str(runtime_plist),
                "desired_state": str(desired_state),
            },
            "last_error": "",
        }
        _atomic_json(transaction_dir / "metadata.json", metadata)
        mutation_started = True

        if runtime_refresh:
            if not target_app.exists() or not _runtime_bundle_present(target_app):
                raise CutoverError("registered Runtime ownership cannot be refreshed without the installed owner app")
            refreshed = _service_management(target_app, "unregister-runtime")
            if refreshed["runtime_agent"] not in ABSENT_SERVICE_STATES:
                raise CutoverError("pre-existing Runtime registration did not unregister for refresh")
            operations["runtime_unregistered"] = True
            _atomic_json(transaction_dir / "metadata.json", metadata)

        target_app.parent.mkdir(parents=True, exist_ok=True)
        if target_app.exists():
            shutil.rmtree(target_app)
        os.replace(staged, target_app)
        _inject(failures, "after_app_swap")
        provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")
        _inject(failures, "after_installed_validation")

        _remove_legacy_predecessor(
            launchctl=launchctl,
            uid=uid,
            ui_plist=ui_plist,
            runtime_plist=runtime_plist,
            ui_present=ui_present,
            runtime_present=runtime_present,
            ui_was_loaded=ui_loaded,
            runtime_was_loaded=runtime_legacy_loaded,
            preserve_modern_runtime=preserve_modern_runtime,
            modern_runtime_program=target_app / "Contents/MacOS/AgentRuntimeRuntimeService",
        )
        _inject(failures, "launchagent_registration")

        candidate_before_registration = _modern_ownership_snapshot(
            target_app, launchctl=launchctl, uid=uid, runtime_legacy_program=None
        )
        candidate_runtime = _validate_runtime_ownership_record(candidate_before_registration["runtime"])

        if candidate_before_registration["main_app"] in ABSENT_SERVICE_STATES:
            state = _service_management(target_app, "register-main")
            if state["main_app"] not in REGISTERED_SERVICE_STATES:
                raise CutoverError("main-app ServiceManagement registration did not converge")
            operations["main_registered"] = True
            _atomic_json(transaction_dir / "metadata.json", metadata)

        if candidate_runtime["classification"] == "stale-registered":
            state = _service_management(target_app, "unregister-runtime")
            if state["runtime_agent"] not in ABSENT_SERVICE_STATES:
                raise CutoverError("stale Runtime registration did not unregister before refresh")
            operations["runtime_unregistered"] = True
            _atomic_json(transaction_dir / "metadata.json", metadata)
            candidate_runtime = dict(candidate_runtime)
            candidate_runtime["classification"] = "absent"
            candidate_runtime["registration_state"] = state["runtime_agent"]

        if candidate_runtime["classification"] == "absent":
            state = _service_management(target_app, "register-runtime")
            if state["runtime_agent"] not in REGISTERED_SERVICE_STATES:
                raise CutoverError("Runtime ServiceManagement registration did not converge")
            operations["runtime_registered"] = True
            _atomic_json(transaction_dir / "metadata.json", metadata)

        post = _modern_ownership_snapshot(
            target_app, launchctl=launchctl, uid=uid, runtime_legacy_program=None
        )
        post_runtime = _validate_runtime_ownership_record(post["runtime"])
        modern_state = {
            "main_app": str(post["main_app"]),
            "runtime_agent": str(post_runtime["registration_state"]),
        }
        metadata["modern_registration"] = modern_state
        metadata["modern_ownership_after"] = post
        _atomic_json(transaction_dir / "metadata.json", metadata)
        if modern_state["main_app"] not in REGISTERED_SERVICE_STATES:
            raise CutoverError("main-app ServiceManagement registration did not converge")
        if modern_state["runtime_agent"] not in REGISTERED_SERVICE_STATES:
            raise CutoverError("Runtime ServiceManagement registration did not converge")
        if modern_state["runtime_agent"] == "enabled" and post_runtime["classification"] != "healthy-registered":
            raise CutoverError("enabled Runtime ServiceManagement state has no healthy loaded Runtime job")
        if modern_state["runtime_agent"] == "requires-approval" and post_runtime["classification"] != "awaiting-approval":
            raise CutoverError("Runtime approval state is inconsistent")
        if _service_loaded(launchctl, ui_service):
            raise CutoverError(f"legacy LaunchAgent remained loaded after modern registration: {ui_service}")
        if ui_plist.exists() or ui_plist.is_symlink() or runtime_plist.exists() or runtime_plist.is_symlink():
            raise CutoverError("legacy LaunchAgent files remained after modern registration")

        _inject(failures, "activation_refresh")
        provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")
        metadata["status"] = "PENDING"
        _atomic_json(transaction_dir / "metadata.json", metadata)
        return {"status": "PENDING", "candidate": expected}
    except Exception as exc:
        if mutation_started and created_transaction and (transaction_dir / "metadata.json").is_file():
            try:
                rollback_transaction(
                    transaction_dir,
                    target_app,
                    launchctl=launchctl,
                    uid=uid,
                    fail_stages=failures,
                )
            except Exception as rollback_exc:
                raise CutoverError(f"cutover failed and rollback incomplete: {rollback_exc}") from exc
            raise CutoverError(f"cutover failed; rollback restored previous state: {exc}") from exc
        if created_transaction and transaction_dir.exists():
            shutil.rmtree(transaction_dir)
        if isinstance(exc, (CutoverError, provenance.PackageProvenanceError)):
            raise CutoverError(str(exc)) from exc
        raise


def _validate_schema1_partial_recovery(
    metadata: dict[str, object],
    transaction_dir: Path,
    target_app: Path,
) -> tuple[dict[str, object], dict[str, str], dict[str, str], dict[str, object]]:
    expected_keys = {
        "schema", "status", "candidate", "previous", "paths",
        "modern_registration_before", "modern_registration", "last_error",
    }
    if set(metadata) != expected_keys or metadata.get("schema") != LEGACY_RECOVERY_SCHEMA:
        raise CutoverError("schema-1 partial recovery transaction shape is unsupported")
    if metadata.get("status") != "PARTIAL":
        raise CutoverError("schema-1 recovery requires a PARTIAL transaction")
    previous = metadata.get("previous")
    if not isinstance(previous, dict) or set(previous) != {
        "app_present", "app_closure", "ui_loaded", "runtime_loaded",
        "desired_state_present", "ui_plist", "runtime_plist",
    }:
        raise CutoverError("schema-1 partial recovery previous-state metadata is malformed")
    if previous.get("app_present") is not True or previous.get("runtime_loaded") is not False:
        raise CutoverError("schema-1 partial recovery does not match the bounded stale-Runtime incident")
    if not isinstance(previous.get("ui_loaded"), bool) or not isinstance(previous.get("desired_state_present"), bool):
        raise CutoverError("schema-1 partial recovery previous-state metadata is malformed")
    for key in ("ui_plist", "runtime_plist"):
        value = previous.get(key)
        if not isinstance(value, dict) or set(value) != {"present", "mode"} or value.get("present") is not True:
            raise CutoverError("schema-1 partial recovery predecessor metadata is malformed")
        if not isinstance(value.get("mode"), int):
            raise CutoverError("schema-1 partial recovery predecessor metadata is malformed")

    before = _validate_recorded_service_state(metadata.get("modern_registration_before"))
    after = _validate_recorded_service_state(metadata.get("modern_registration"))
    if before["main_app"] not in ABSENT_SERVICE_STATES or before["runtime_agent"] != "enabled":
        raise CutoverError("schema-1 partial recovery does not match the bounded stale-Runtime incident")
    if after["main_app"] not in REGISTERED_SERVICE_STATES or after["runtime_agent"] not in REGISTERED_SERVICE_STATES:
        raise CutoverError("schema-1 partial recovery registration evidence is malformed")

    paths = metadata.get("paths")
    if not isinstance(paths, dict) or set(paths) != {"target_app", "ui_plist", "runtime_plist", "desired_state"}:
        raise CutoverError("schema-1 partial recovery paths are malformed")
    if Path(str(paths["target_app"])) != target_app:
        raise CutoverError("schema-1 partial recovery target does not match the installed app")
    if Path(str(paths["ui_plist"])).name != f"{UI_LABEL}.plist" or Path(str(paths["runtime_plist"])).name != f"{RUNTIME_LABEL}.plist":
        raise CutoverError("schema-1 partial recovery predecessor paths are malformed")
    if Path(str(paths["desired_state"])).name != "protected-runtime-running":
        raise CutoverError("schema-1 partial recovery desired-state path is malformed")

    candidate = metadata.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("bundle_identifier") != APP_BUNDLE_IDENTIFIER:
        raise CutoverError("schema-1 partial recovery candidate identity is malformed")
    return previous, before, after, paths


def _validate_recovery_snapshot_signature(
    backup: Path,
    expected_closure: dict[str, object],
    expected_team: str,
) -> None:
    if _rollback_app_closure(backup) != expected_closure:
        raise CutoverError("rollback app snapshot closure does not match recovery envelope")
    provenance._verify_codesign(backup)
    main_team = provenance._codesign_team_identifier(backup)
    helper = backup / "Contents/MacOS/AgentRuntimeRuntimeService"
    helper_team = provenance._codesign_team_identifier(helper)
    if not expected_team or main_team != expected_team or helper_team != expected_team:
        raise CutoverError("rollback app snapshot signing identity does not match candidate ownership")


def _recover_schema1_partial(
    transaction_dir: Path,
    target_app: Path,
    metadata: dict[str, object],
    *,
    launchctl: Path,
    uid: int,
    fail_stages: set[str],
) -> None:
    previous, before, after, paths = _validate_schema1_partial_recovery(metadata, transaction_dir, target_app)
    if target_app.is_symlink() or not target_app.is_dir():
        raise CutoverError("installed app path is unsafe before partial recovery")
    provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")

    expected_closure = _require_rollback_app_closure(previous.get("app_closure"))
    backup = transaction_dir / "previous-app"
    candidate = metadata["candidate"]
    assert isinstance(candidate, dict)
    expected_team = candidate.get("team_identifier")
    if not isinstance(expected_team, str):
        raise CutoverError("schema-1 partial recovery candidate signing identity is malformed")
    _validate_recovery_snapshot_signature(backup, expected_closure, expected_team)

    runtime_service = f"gui/{uid}/{RUNTIME_LABEL}"
    if _service_loaded(launchctl, runtime_service):
        raise CutoverError("schema-1 partial recovery requires the modern Runtime job to be absent")
    current = _service_management(target_app, "status")
    if current["runtime_agent"] not in ABSENT_SERVICE_STATES:
        raise CutoverError("schema-1 partial recovery refuses to invent Runtime unregister authority")
    if current["main_app"] in REGISTERED_SERVICE_STATES:
        current = _service_management(target_app, "unregister-main")
        if current["main_app"] not in ABSENT_SERVICE_STATES:
            raise CutoverError("transaction-created main-app registration could not be removed")
    elif current["main_app"] not in ABSENT_SERVICE_STATES:
        raise CutoverError("schema-1 partial recovery main-app state is ambiguous")

    _inject(fail_stages, "recover_restore_app")
    shutil.rmtree(target_app)
    _copy_rollback_app(backup, target_app)
    if _rollback_app_closure(target_app) != expected_closure:
        raise CutoverError("restored previous app closure does not match recovery envelope")

    ui_plist = Path(str(paths["ui_plist"]))
    runtime_plist = Path(str(paths["runtime_plist"]))
    desired_state = Path(str(paths["desired_state"]))
    _restore_file(previous["ui_plist"], transaction_dir / "previous-ui.plist", ui_plist)
    _restore_file(previous["runtime_plist"], transaction_dir / "previous-runtime.plist", runtime_plist)
    desired_before = bool(previous["desired_state_present"])
    if desired_before:
        desired_state.parent.mkdir(parents=True, exist_ok=True)
        desired_state.touch(exist_ok=True)
    elif desired_state.exists() or desired_state.is_symlink():
        if desired_state.is_symlink() or not desired_state.is_file():
            raise CutoverError("desired-state recovery target is unsafe")
        desired_state.unlink()

    _restore_loaded_state(
        launchctl=launchctl,
        uid=uid,
        ui_plist=ui_plist,
        runtime_plist=runtime_plist,
        ui_loaded=bool(previous["ui_loaded"]),
        runtime_loaded=False,
    )
    if _service_loaded(launchctl, runtime_service):
        raise CutoverError("stale pre-existing Runtime registration was recreated during recovery")


def recover_partial_transaction(
    transaction_dir: Path,
    target_app: Path,
    *,
    launchctl: Path,
    uid: int,
    fail_stages: set[str] | None = None,
) -> dict[str, object]:
    failures = set(fail_stages or ())
    metadata = _load_metadata(
        transaction_dir, allowed_schemas={LEGACY_RECOVERY_SCHEMA, TRANSACTION_SCHEMA}
    )
    if metadata.get("status") != "PARTIAL":
        raise CutoverError("partial recovery requires a PARTIAL cutover transaction")
    try:
        if metadata["schema"] == TRANSACTION_SCHEMA:
            _restore_transaction(
                transaction_dir, target_app, launchctl=launchctl, uid=uid, fail_stages=failures
            )
        elif metadata["schema"] == LEGACY_RECOVERY_SCHEMA:
            _recover_schema1_partial(
                transaction_dir, target_app, metadata, launchctl=launchctl, uid=uid, fail_stages=failures
            )
        else:
            raise CutoverError("cutover transaction metadata schema is invalid")
    except Exception as exc:
        metadata["status"] = "PARTIAL"
        metadata["last_error"] = "recovery incomplete: " + str(exc)
        _atomic_json(transaction_dir / "metadata.json", metadata)
        if isinstance(exc, CutoverError):
            raise CutoverError("recovery incomplete: " + str(exc)) from exc
        raise CutoverError("recovery incomplete") from exc
    shutil.rmtree(transaction_dir)
    return {"status": "RECOVERED"}


def commit_transaction(transaction_dir: Path, target_app: Path) -> dict[str, object]:
    metadata = _load_metadata(transaction_dir)
    if metadata.get("status") != "PENDING":
        raise CutoverError("only a pending cutover transaction can be committed")
    expected_target = Path(str(metadata.get("paths", {}).get("target_app", "")))
    if expected_target != target_app:
        raise CutoverError("commit target does not match pending cutover transaction")
    provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")
    shutil.rmtree(transaction_dir)
    return {"status": "COMMITTED"}


def _defaults(home: Path) -> tuple[Path, Path, Path, Path, Path]:
    target_app = home / "Applications" / "Agent Runtime.app"
    launch_dir = home / "Library" / "LaunchAgents"
    state_dir = home / "Library" / "Application Support" / "Agent Runtime"
    return (
        target_app,
        launch_dir / f"{UI_LABEL}.plist",
        launch_dir / f"{RUNTIME_LABEL}.plist",
        state_dir,
        state_dir / "cutover-transaction",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cutover = sub.add_parser("cutover")
    cutover.add_argument("candidate", type=Path)
    cutover.add_argument("handoff", type=Path)
    cutover.add_argument("--home", type=Path, default=Path.home())
    cutover.add_argument("--launchctl", type=Path, required=True)
    cutover.add_argument("--tunnel-client", type=Path, required=True)
    cutover.add_argument("--uid", type=int, default=os.getuid())
    commit = sub.add_parser("commit")
    commit.add_argument("--home", type=Path, default=Path.home())
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--home", type=Path, default=Path.home())
    rollback.add_argument("--launchctl", type=Path, required=True)
    rollback.add_argument("--uid", type=int, default=os.getuid())
    recover = sub.add_parser("recover")
    recover.add_argument("--home", type=Path, default=Path.home())
    recover.add_argument("--launchctl", type=Path, required=True)
    recover.add_argument("--uid", type=int, default=os.getuid())
    args = parser.parse_args()
    target_app, ui_plist, runtime_plist, state_dir, transaction_dir = _defaults(args.home)
    try:
        if args.command == "cutover":
            result = cutover_candidate(
                args.candidate,
                args.handoff,
                target_app=target_app,
                ui_plist=ui_plist,
                runtime_plist=runtime_plist,
                state_dir=state_dir,
                transaction_dir=transaction_dir,
                home=args.home,
                launchctl=args.launchctl,
                tunnel_client=args.tunnel_client,
                uid=args.uid,
            )
        elif args.command == "commit":
            result = commit_transaction(transaction_dir, target_app)
        elif args.command == "rollback":
            result = rollback_transaction(
                transaction_dir,
                target_app,
                launchctl=args.launchctl,
                uid=args.uid,
            )
        else:
            result = recover_partial_transaction(
                transaction_dir,
                target_app,
                launchctl=args.launchctl,
                uid=args.uid,
            )
    except (CutoverError, provenance.PackageProvenanceError) as exc:
        print("CUTOVER ERROR: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
