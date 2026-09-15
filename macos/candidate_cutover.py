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

TRANSACTION_SCHEMA = 1
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


def _refresh_service_registration(
    *,
    launchctl: Path,
    domain: str,
    service: str,
    plist: Path,
    expected_label: str,
    was_loaded: bool,
    unregister_message: str,
    register_message: str,
) -> None:
    if was_loaded:
        _require_launchctl_ok(
            _run([str(launchctl), "bootout", service]),
            unregister_message,
        )
        _wait_for_service_absence(launchctl, service)
    _require_launchctl_ok(
        _run([str(launchctl), "bootstrap", domain, str(plist)]),
        register_message,
    )
    _require_service_identity(launchctl, service, _launchagent_program(plist, expected_label))


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)


def _load_metadata(transaction_dir: Path) -> dict[str, object]:
    path = transaction_dir / "metadata.json"
    if path.is_symlink() or not path.is_file():
        raise CutoverError("cutover transaction metadata is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CutoverError("cutover transaction metadata is malformed") from exc
    if not isinstance(value, dict) or value.get("schema") != TRANSACTION_SCHEMA:
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


def _ui_plist(target_app: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": UI_LABEL,
            "AssociatedBundleIdentifiers": [APP_BUNDLE_IDENTIFIER],
            "ProgramArguments": [str(target_app / "Contents/MacOS/AgentRuntimeMenuBar")],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
        },
        fmt=plistlib.FMT_XML,
    )


def _runtime_plist(target_app: Path, home: Path, tunnel_client: Path, desired_state: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": RUNTIME_LABEL,
            "AssociatedBundleIdentifiers": [APP_BUNDLE_IDENTIFIER],
            "ProgramArguments": [
                str(target_app / "Contents/Resources/runtime/start.sh"),
                "--serve",
                str(tunnel_client),
            ],
            "EnvironmentVariables": {"HOME": str(home), "PATH": RUNTIME_PATH},
            "RunAtLoad": False,
            "KeepAlive": {"PathState": {str(desired_state): True}},
            "ProcessType": "Interactive",
            "ThrottleInterval": 2,
        },
        fmt=plistlib.FMT_XML,
    )


def _atomic_file(path: Path, payload: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise CutoverError(f"refusing to replace symlinked file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".cutover")
    temp.write_bytes(payload)
    temp.chmod(mode)
    os.replace(temp, path)


def _register_services(
    *,
    launchctl: Path,
    uid: int,
    ui_plist: Path,
    runtime_plist: Path,
    ui_was_loaded: bool,
    runtime_was_loaded: bool,
    desired_state_present: bool,
    fail_stages: set[str],
) -> None:
    domain = f"gui/{uid}"
    ui_service = f"{domain}/{UI_LABEL}"
    runtime_service = f"{domain}/{RUNTIME_LABEL}"
    _refresh_service_registration(
        launchctl=launchctl,
        domain=domain,
        service=ui_service,
        plist=ui_plist,
        expected_label=UI_LABEL,
        was_loaded=ui_was_loaded,
        unregister_message="could not unregister the previous menu-bar LaunchAgent",
        register_message="could not register the menu-bar LaunchAgent",
    )

    _inject(fail_stages, "launchagent_registration")
    _refresh_service_registration(
        launchctl=launchctl,
        domain=domain,
        service=runtime_service,
        plist=runtime_plist,
        expected_label=RUNTIME_LABEL,
        was_loaded=runtime_was_loaded,
        unregister_message="could not unregister the previous Runtime LaunchAgent",
        register_message="could not register the Runtime LaunchAgent",
    )

    _inject(fail_stages, "activation_refresh")
    if desired_state_present:
        _require_launchctl_ok(
            _run([str(launchctl), "kickstart", "-k", runtime_service]),
            "could not refresh the desired Runtime generation",
        )

    _require_service_identity(launchctl, ui_service, _launchagent_program(ui_plist, UI_LABEL))
    _require_service_identity(launchctl, runtime_service, _launchagent_program(runtime_plist, RUNTIME_LABEL))


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
    _inject(fail_stages, "rollback_restore_app")

    if target_app.is_symlink():
        raise CutoverError("installed app became a symlink before rollback")
    if app_present:
        expected_closure = _require_rollback_app_closure(previous.get("app_closure"))
        backup = transaction_dir / "previous-app"
        if _rollback_app_closure(backup) != expected_closure:
            raise CutoverError("rollback app snapshot closure does not match rollback envelope")
    if target_app.exists():
        if not target_app.is_dir():
            raise CutoverError("installed app path is not a directory during rollback")
        shutil.rmtree(target_app)
    if app_present:
        _copy_rollback_app(backup, target_app)
        if _rollback_app_closure(target_app) != expected_closure:
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
    previous: dict[str, object] = {
        "app_present": False,
        "app_closure": None,
        "ui_loaded": _service_loaded(launchctl, ui_service),
        "runtime_loaded": _service_loaded(launchctl, runtime_service),
        "desired_state_present": desired_state.exists(),
    }
    if desired_state.is_symlink() or (desired_state.exists() and not desired_state.is_file()):
        raise CutoverError("desired Runtime state marker is unsafe")
    ui_present = _validate_previous_launchagent(ui_plist, UI_LABEL, target_app)
    runtime_present = _validate_previous_launchagent(runtime_plist, RUNTIME_LABEL, target_app)
    if previous["ui_loaded"] and not ui_present:
        raise CutoverError("previous UI LaunchAgent is loaded but its plist is unavailable")
    if previous["runtime_loaded"] and not runtime_present:
        raise CutoverError("previous Runtime LaunchAgent is loaded but its plist is unavailable")
    if (ui_present or runtime_present) and not target_app.exists():
        raise CutoverError("previous LaunchAgent state exists without an installed app rollback target")

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
        target_app.parent.mkdir(parents=True, exist_ok=True)
        if target_app.exists():
            shutil.rmtree(target_app)
        os.replace(staged, target_app)
        _inject(failures, "after_app_swap")

        provenance.validate_candidate(target_app, transaction_dir / "candidate-handoff.json")
        _inject(failures, "after_installed_validation")

        _atomic_file(ui_plist, _ui_plist(target_app))
        _atomic_file(runtime_plist, _runtime_plist(target_app, home, tunnel_client, desired_state))
        _register_services(
            launchctl=launchctl,
            uid=uid,
            ui_plist=ui_plist,
            runtime_plist=runtime_plist,
            ui_was_loaded=bool(previous["ui_loaded"]),
            runtime_was_loaded=bool(previous["runtime_loaded"]),
            desired_state_present=bool(previous["desired_state_present"]),
            fail_stages=failures,
        )
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
        else:
            result = rollback_transaction(
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
