#!/usr/bin/env python3
"""Transactional installation of an already-sealed Agent Runtime.app candidate."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

MACOS_ROOT = Path(__file__).resolve().parent
if str(MACOS_ROOT) not in sys.path:
    sys.path.insert(0, str(MACOS_ROOT))
import package_provenance as provenance

TRANSACTION_SCHEMA = 1
UI_LABEL = "com.picmao.agent-runtime-ui"
RUNTIME_LABEL = "com.picmao.agent-runtime-runtime"
RUNTIME_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


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


def _service_loaded(launchctl: Path, service: str) -> bool:
    return _run([str(launchctl), "print", service]).returncode == 0


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


def _verify_owned_app(app: Path) -> dict[str, object]:
    provenance._verify_codesign(app)
    identity = provenance.embedded_candidate_identity(app)
    closure = provenance.candidate_closure(app)
    provenance._verify_codesign(app)
    return {**identity, **closure}


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


def _require_launchctl_ok(result: subprocess.CompletedProcess[str], message: str) -> None:
    if result.returncode != 0:
        raise CutoverError(message)


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
    if ui_was_loaded:
        _require_launchctl_ok(
            _run([str(launchctl), "kickstart", "-k", ui_service]),
            "could not refresh the menu-bar LaunchAgent",
        )
    else:
        _require_launchctl_ok(
            _run([str(launchctl), "bootstrap", domain, str(ui_plist)]),
            "could not register the menu-bar LaunchAgent",
        )

    _inject(fail_stages, "launchagent_registration")
    if runtime_was_loaded:
        _require_launchctl_ok(
            _run([str(launchctl), "bootout", runtime_service]),
            "could not unregister the previous Runtime LaunchAgent",
        )
    _require_launchctl_ok(
        _run([str(launchctl), "bootstrap", domain, str(runtime_plist)]),
        "could not register the Runtime LaunchAgent",
    )

    _inject(fail_stages, "activation_refresh")
    if desired_state_present:
        _require_launchctl_ok(
            _run([str(launchctl), "kickstart", "-k", runtime_service]),
            "could not refresh the desired Runtime generation",
        )

    if not _service_loaded(launchctl, ui_service) or not _service_loaded(launchctl, runtime_service):
        raise CutoverError("LaunchAgent registration did not reach the expected loaded state")


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
        if expected_loaded:
            _require_launchctl_ok(
                _run([str(launchctl), "bootstrap", domain, str(plist)]),
                f"could not restore service during rollback: {service}",
            )
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
    _inject(fail_stages, "rollback_restore_app")

    if target_app.is_symlink():
        raise CutoverError("installed app became a symlink before rollback")
    if target_app.exists():
        if not target_app.is_dir():
            raise CutoverError("installed app path is not a directory during rollback")
        shutil.rmtree(target_app)
    if previous.get("app_present"):
        backup = transaction_dir / "previous-app"
        _copy_app(backup, target_app)
        actual = _verify_owned_app(target_app)
        if actual != previous.get("app_identity"):
            raise CutoverError("restored previous app identity does not match rollback envelope")

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
        "app_identity": None,
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
            previous_identity = _verify_owned_app(target_app)
            _copy_app(target_app, transaction_dir / "previous-app")
            if _verify_owned_app(transaction_dir / "previous-app") != previous_identity:
                raise CutoverError("previous app rollback copy changed identity")
            previous["app_present"] = True
            previous["app_identity"] = previous_identity

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
