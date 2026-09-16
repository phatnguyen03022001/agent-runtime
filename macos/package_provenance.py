#!/usr/bin/env python3
"""Immutable source staging and closed-world Runtime payload provenance."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import plistlib
import re
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

SCHEMA = 1
CANDIDATE_SCHEMA = 2
OWNER = "com.picmao.agent-runtime"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MANIFEST_KEYS = {
    "schema",
    "owner",
    "runtime_revision",
    "git_tree",
    "requirements_lock_sha256",
    "entrypoint",
    "python",
    "mcp_package",
    "files",
    "payload_sha256",
}
ENTRY_KEYS = {"path", "size", "sha256"}
CANDIDATE_KEYS = {
    "schema",
    "bundle_identifier",
    "source_revision",
    "source_tree",
    "requirements_lock_sha256",
    "team_identifier",
    "main_executable",
    "runtime_service_executable",
    "record_count",
    "candidate_sha256",
}


class PackageProvenanceError(RuntimeError):
    pass


def _run_git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace") if binary else result.stderr
        raise PackageProvenanceError("git command failed: " + detail.strip())
    return result.stdout


def git_identity(repo: Path) -> tuple[str, str]:
    status_text = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status_text.strip():
        raise PackageProvenanceError("release packaging requires a clean Git checkout")
    revision = _run_git(repo, "rev-parse", "--verify", "HEAD").strip()
    tree = _run_git(repo, "rev-parse", "--verify", "HEAD^{tree}").strip()
    if HEX40.fullmatch(revision) is None or HEX40.fullmatch(tree) is None:
        raise PackageProvenanceError("Git revision/tree identity is invalid")
    return revision, tree


def export_head(repo: Path, destination: Path) -> tuple[str, str]:
    revision, tree = git_identity(repo)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise PackageProvenanceError("immutable source staging directory must be empty")
    archive = _run_git(repo, "archive", "--format=tar", "HEAD", binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        members = bundle.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise PackageProvenanceError("Git archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise PackageProvenanceError("Git archive source staging must not contain symlinks")
            if not (member.isfile() or member.isdir()):
                raise PackageProvenanceError("Git archive source staging contains a non-regular entry")
        bundle.extractall(destination, members=members, filter="data")
    return revision, tree


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lock_sha256(lock_path: Path) -> str:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise PackageProvenanceError("requirements.lock must be a regular non-symlink file")
    return _sha256(lock_path)


def validate_candidate_relative_path(path: str) -> bytes:
    if not isinstance(path, str) or not path or any(ch in path for ch in "\x00\t\r\n"):
        raise PackageProvenanceError("candidate file path is invalid")
    pure = PurePosixPath(path)
    if pure.is_absolute() or path != pure.as_posix() or path == "." or any(part in {".", ".."} for part in pure.parts):
        raise PackageProvenanceError("candidate file path is invalid")
    try:
        return path.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise PackageProvenanceError("candidate file path is not valid UTF-8") from exc


def _candidate_records(app: Path) -> list[tuple[bytes, str]]:
    if app.is_symlink() or not app.is_dir():
        raise PackageProvenanceError("candidate app root must be a regular directory")
    records: list[tuple[bytes, str]] = []
    seen: set[str] = set()
    for current, dirs, files in os.walk(app, topdown=True, followlinks=False):
        root = Path(current)
        for name in dirs:
            candidate = root / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PackageProvenanceError("candidate app must not contain symlinks")
            if not stat.S_ISDIR(info.st_mode):
                raise PackageProvenanceError("candidate app contains an unsupported non-regular entry")
        for name in files:
            candidate = root / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PackageProvenanceError("candidate app must not contain symlinks")
            if not stat.S_ISREG(info.st_mode):
                raise PackageProvenanceError("candidate app contains an unsupported non-regular entry")
            relative = candidate.relative_to(app).as_posix()
            path_bytes = validate_candidate_relative_path(relative)
            if relative in seen:
                raise PackageProvenanceError("candidate app contains duplicate file paths")
            seen.add(relative)
            mode = stat.S_IMODE(info.st_mode)
            record = f"{relative}\t{mode:04o}\t{info.st_size}\t{_sha256(candidate)}\n"
            records.append((path_bytes, record))
    records.sort(key=lambda item: item[0])
    return records


def candidate_closure(app: Path) -> dict[str, object]:
    records = _candidate_records(app)
    canonical = "".join(record for _, record in records).encode("utf-8")
    return {
        "record_count": len(records),
        "candidate_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _runtime_entries(runtime: Path) -> list[dict[str, object]]:
    if runtime.is_symlink() or not runtime.is_dir():
        raise PackageProvenanceError("runtime payload root must be a regular directory")
    entries: list[dict[str, object]] = []
    for current, dirs, files in os.walk(runtime, topdown=True, followlinks=False):
        root = Path(current)
        for name in list(dirs):
            candidate = root / name
            if candidate.is_symlink():
                raise PackageProvenanceError("runtime payload must not contain symlinks")
        for name in files:
            candidate = root / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PackageProvenanceError("runtime payload must not contain symlinks")
            if not stat.S_ISREG(info.st_mode):
                raise PackageProvenanceError("runtime payload contains a non-regular file")
            relative = candidate.relative_to(runtime).as_posix()
            entries.append({"path": relative, "size": info.st_size, "sha256": _sha256(candidate)})
    entries.sort(key=lambda entry: str(entry["path"]))
    return entries


def aggregate_digest(entries: list[dict[str, object]]) -> str:
    canonical = "".join(
        f"{entry['path']}\t{entry['size']}\t{entry['sha256']}\n" for entry in entries
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_identity(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PackageProvenanceError(f"manifest {label} identity is invalid")
    return value


def write_manifest(
    runtime: Path,
    manifest_path: Path,
    revision: str,
    tree: str,
    lock_sha: str,
) -> dict[str, object]:
    _validate_identity(revision, HEX40, "revision")
    _validate_identity(tree, HEX40, "tree")
    _validate_identity(lock_sha, HEX64, "requirements.lock")
    entries = _runtime_entries(runtime)
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "owner": OWNER,
        "runtime_revision": revision,
        "git_tree": tree,
        "requirements_lock_sha256": lock_sha,
        "entrypoint": "runtime/start.sh",
        "python": "runtime/.venv/bin/python",
        "mcp_package": "runtime/agent_runtime",
        "files": entries,
        "payload_sha256": aggregate_digest(entries),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def _load_manifest(manifest_path: Path) -> dict[str, object]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PackageProvenanceError("runtime manifest must be a regular non-symlink file")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageProvenanceError("runtime manifest is malformed") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        raise PackageProvenanceError("runtime manifest shape is invalid")
    return value


def validate_manifest(
    runtime: Path,
    manifest_path: Path,
    expected_revision: str,
    expected_tree: str,
    expected_lock_sha: str,
) -> dict[str, object]:
    _validate_identity(expected_revision, HEX40, "expected revision")
    _validate_identity(expected_tree, HEX40, "expected tree")
    _validate_identity(expected_lock_sha, HEX64, "expected requirements.lock")
    manifest = _load_manifest(manifest_path)
    if manifest["schema"] != SCHEMA or manifest["owner"] != OWNER:
        raise PackageProvenanceError("runtime manifest ownership/schema is invalid")
    if manifest["entrypoint"] != "runtime/start.sh" or manifest["python"] != "runtime/.venv/bin/python":
        raise PackageProvenanceError("runtime manifest execution paths are invalid")
    if manifest["mcp_package"] != "runtime/agent_runtime":
        raise PackageProvenanceError("runtime manifest MCP package path is invalid")
    revision = _validate_identity(manifest["runtime_revision"], HEX40, "revision")
    tree = _validate_identity(manifest["git_tree"], HEX40, "tree")
    lock_sha = _validate_identity(manifest["requirements_lock_sha256"], HEX64, "requirements.lock")
    payload_sha = _validate_identity(manifest["payload_sha256"], HEX64, "payload")
    if revision != expected_revision or tree != expected_tree or lock_sha != expected_lock_sha:
        raise PackageProvenanceError("runtime manifest provenance identity mismatch")

    files = manifest["files"]
    if not isinstance(files, list):
        raise PackageProvenanceError("runtime manifest files must be a list")
    normalized: list[dict[str, object]] = []
    previous = None
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise PackageProvenanceError("runtime manifest file entry is malformed")
        path = entry["path"]
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(path, str) or not path or "\\" in path:
            raise PackageProvenanceError("runtime manifest file path is invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or path != pure.as_posix() or ".." in pure.parts or "." in pure.parts:
            raise PackageProvenanceError("runtime manifest file path is invalid")
        if path in seen:
            raise PackageProvenanceError("runtime manifest contains duplicate file paths")
        if previous is not None and path <= previous:
            raise PackageProvenanceError("runtime manifest file entries are not strictly sorted")
        if type(size) is not int or size < 0:
            raise PackageProvenanceError("runtime manifest file size is invalid")
        _validate_identity(digest, HEX64, "file hash")
        seen.add(path)
        previous = path
        normalized.append({"path": path, "size": size, "sha256": digest})

    actual = _runtime_entries(runtime)
    if normalized != actual:
        raise PackageProvenanceError("runtime manifest does not exactly close over the payload")
    if aggregate_digest(normalized) != payload_sha:
        raise PackageProvenanceError("runtime manifest aggregate payload digest mismatch")
    return manifest


def _verify_codesign(app: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise PackageProvenanceError("candidate failed strict deep code-signature verification")


MAIN_EXECUTABLE_RELATIVE = "Contents/MacOS/AgentRuntimeMenuBar"
RUNTIME_SERVICE_EXECUTABLE_RELATIVE = "Contents/MacOS/AgentRuntimeRuntimeService"


def _codesign_team_identifier(path: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise PackageProvenanceError(f"could not inspect code-signing identity for {path.name}")
    detail = result.stdout + "\n" + result.stderr
    match = re.search(r"(?m)^TeamIdentifier=(.+)$", detail)
    if match is None:
        raise PackageProvenanceError(f"TeamIdentifier is missing for {path.name}")
    team = match.group(1).strip()
    if not team or team.lower() in {"not set", "none", "null"}:
        raise PackageProvenanceError(f"TeamIdentifier is missing for {path.name}")
    return team


def responsible_code_identity(app: Path, team_identifier_reader=None) -> dict[str, object]:
    info_path = app / "Contents" / "Info.plist"
    if info_path.is_symlink() or not info_path.is_file():
        raise PackageProvenanceError("candidate Info.plist must be a regular non-symlink file")
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise PackageProvenanceError("candidate Info.plist is malformed") from exc
    if info.get("CFBundleIdentifier") != OWNER or info.get("CFBundleExecutable") != "AgentRuntimeMenuBar":
        raise PackageProvenanceError("candidate main app identity is not owned by agent-runtime")
    main = app / MAIN_EXECUTABLE_RELATIVE
    runtime_service = app / RUNTIME_SERVICE_EXECUTABLE_RELATIVE
    for target in (main, runtime_service):
        if target.is_symlink() or not target.is_file():
            raise PackageProvenanceError(f"responsible executable is missing or unsafe: {target.name}")
    reader = team_identifier_reader or _codesign_team_identifier
    main_team = reader(main)
    runtime_team = reader(runtime_service)
    if not isinstance(main_team, str) or not main_team.strip():
        raise PackageProvenanceError("main app TeamIdentifier is missing")
    if not isinstance(runtime_team, str) or not runtime_team.strip():
        raise PackageProvenanceError("Runtime responsible executable TeamIdentifier is missing")
    if main_team != runtime_team:
        raise PackageProvenanceError("responsible executable TeamIdentifier does not match main app TeamIdentifier")
    return {
        "team_identifier": main_team,
        "main_executable": MAIN_EXECUTABLE_RELATIVE,
        "runtime_service_executable": RUNTIME_SERVICE_EXECUTABLE_RELATIVE,
    }


def embedded_candidate_identity(app: Path) -> dict[str, object]:
    info_path = app / "Contents" / "Info.plist"
    if info_path.is_symlink() or not info_path.is_file():
        raise PackageProvenanceError("candidate Info.plist must be a regular non-symlink file")
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise PackageProvenanceError("candidate Info.plist is malformed") from exc
    if info.get("CFBundleIdentifier") != OWNER:
        raise PackageProvenanceError("candidate bundle identifier is not owned by agent-runtime")

    runtime = app / "Contents" / "Resources" / "runtime"
    manifest_path = app / "Contents" / "Resources" / "runtime-manifest.json"
    manifest = _load_manifest(manifest_path)
    revision = _validate_identity(manifest.get("runtime_revision"), HEX40, "revision")
    tree = _validate_identity(manifest.get("git_tree"), HEX40, "tree")
    lock_sha = _validate_identity(manifest.get("requirements_lock_sha256"), HEX64, "requirements.lock")
    validate_manifest(runtime, manifest_path, revision, tree, lock_sha)
    return {
        "bundle_identifier": OWNER,
        "source_revision": revision,
        "source_tree": tree,
        "requirements_lock_sha256": lock_sha,
        **responsible_code_identity(app),
    }


def _candidate_handoff_data(app: Path) -> dict[str, object]:
    identity = embedded_candidate_identity(app)
    closure = candidate_closure(app)
    return {
        "schema": CANDIDATE_SCHEMA,
        **identity,
        **closure,
    }


def _load_candidate_handoff(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PackageProvenanceError("candidate handoff must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageProvenanceError("candidate handoff is malformed") from exc
    if not isinstance(value, dict) or set(value) != CANDIDATE_KEYS:
        raise PackageProvenanceError("candidate handoff shape is invalid")
    if value["schema"] != CANDIDATE_SCHEMA or value["bundle_identifier"] != OWNER:
        raise PackageProvenanceError("candidate handoff ownership/schema is invalid")
    _validate_identity(value["source_revision"], HEX40, "candidate revision")
    _validate_identity(value["source_tree"], HEX40, "candidate tree")
    _validate_identity(value["requirements_lock_sha256"], HEX64, "candidate requirements.lock")
    team = value["team_identifier"]
    if not isinstance(team, str) or not team.strip() or team.lower() in {"not set", "none", "null"}:
        raise PackageProvenanceError("candidate TeamIdentifier is invalid")
    if value["main_executable"] != MAIN_EXECUTABLE_RELATIVE:
        raise PackageProvenanceError("candidate main executable identity is invalid")
    if value["runtime_service_executable"] != RUNTIME_SERVICE_EXECUTABLE_RELATIVE:
        raise PackageProvenanceError("candidate Runtime responsible executable identity is invalid")
    _validate_identity(value["candidate_sha256"], HEX64, "candidate closure")
    if type(value["record_count"]) is not int or value["record_count"] < 1:
        raise PackageProvenanceError("candidate handoff record count is invalid")
    return value


def validate_candidate(app: Path, handoff_path: Path) -> dict[str, object]:
    expected = _load_candidate_handoff(handoff_path)
    _verify_codesign(app)
    actual = _candidate_handoff_data(app)
    if actual != expected:
        raise PackageProvenanceError("candidate identity does not match expected external handoff")
    _verify_codesign(app)
    final = _candidate_handoff_data(app)
    if final != expected:
        raise PackageProvenanceError("candidate changed during validation")
    return expected


def seal_candidate(app: Path, handoff_path: Path) -> dict[str, object]:
    app_real = app.resolve()
    handoff_real = handoff_path.resolve(strict=False)
    if handoff_real == app_real or app_real in handoff_real.parents:
        raise PackageProvenanceError("candidate handoff must remain external to the app bundle")
    _verify_codesign(app)
    data = _candidate_handoff_data(app)
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = handoff_path.with_name(handoff_path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, handoff_path)
    validate_candidate(app, handoff_path)
    return data


def publish_candidate(app: Path, handoff_path: Path, candidates_root: Path) -> dict[str, object]:
    if app.name != "Agent Runtime.app" or handoff_path.name != "Agent Runtime.candidate.json":
        raise PackageProvenanceError("candidate publication paths use unexpected names")
    if app.parent != handoff_path.parent:
        raise PackageProvenanceError("candidate app and handoff must share one staging directory")
    staging_root = app.parent
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise PackageProvenanceError("candidate staging directory must be a regular directory")

    candidate = validate_candidate(app, handoff_path)
    candidate_sha = _validate_identity(candidate.get("candidate_sha256"), HEX64, "candidate closure")
    final_root = candidates_root / candidate_sha
    if final_root.exists() or final_root.is_symlink():
        raise PackageProvenanceError("candidate output already exists")

    candidates_root.mkdir(parents=True, exist_ok=True)
    if final_root.exists() or final_root.is_symlink():
        raise PackageProvenanceError("candidate output already exists")
    try:
        os.rename(staging_root, final_root)
    except FileExistsError as exc:
        raise PackageProvenanceError("candidate output already exists") from exc
    except OSError as exc:
        raise PackageProvenanceError("candidate publication failed: " + str(exc)) from exc

    final_app = final_root / app.name
    final_handoff = final_root / handoff_path.name
    return {
        **candidate,
        "candidate_app": str(final_app),
        "candidate_handoff": str(final_handoff),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("repo", type=Path)
    stage.add_argument("destination", type=Path)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("runtime", type=Path)
    manifest.add_argument("manifest_path", type=Path)
    manifest.add_argument("revision")
    manifest.add_argument("tree")
    manifest.add_argument("lock", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("runtime", type=Path)
    validate.add_argument("manifest_path", type=Path)
    validate.add_argument("revision")
    validate.add_argument("tree")
    validate.add_argument("lock", type=Path)
    seal = subparsers.add_parser("seal")
    seal.add_argument("app", type=Path)
    seal.add_argument("handoff", type=Path)
    publish = subparsers.add_parser("publish")
    publish.add_argument("app", type=Path)
    publish.add_argument("handoff", type=Path)
    publish.add_argument("candidates_root", type=Path)
    candidate_validate = subparsers.add_parser("validate-candidate")
    candidate_validate.add_argument("app", type=Path)
    candidate_validate.add_argument("handoff", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "stage":
            revision, tree = export_head(args.repo, args.destination)
            print(f"{revision}\t{tree}")
        elif args.command == "manifest":
            write_manifest(args.runtime, args.manifest_path, args.revision, args.tree, lock_sha256(args.lock))
        elif args.command == "validate":
            validate_manifest(
                args.runtime,
                args.manifest_path,
                args.revision,
                args.tree,
                lock_sha256(args.lock),
            )
        elif args.command == "seal":
            print(json.dumps(seal_candidate(args.app, args.handoff), sort_keys=True))
        elif args.command == "publish":
            published = publish_candidate(args.app, args.handoff, args.candidates_root)
            print(
                f"{published['candidate_app']}\t{published['candidate_handoff']}\t"
                f"{published['candidate_sha256']}"
            )
        else:
            print(json.dumps(validate_candidate(args.app, args.handoff), sort_keys=True))
    except PackageProvenanceError as exc:
        print("PROVENANCE ERROR: " + str(exc), file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
