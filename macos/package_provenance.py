#!/usr/bin/env python3
"""Immutable source staging and closed-world Runtime payload provenance."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

LEGACY_SCHEMA = 1
SCHEMA = 2
CANDIDATE_SCHEMA = 2
OWNER = "com.picmao.agent-runtime"
SERVICE_MANAGEMENT_CONTRACT = "split-v1"
SERVICE_MANAGEMENT_CONTRACTS = {SERVICE_MANAGEMENT_CONTRACT}
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MANIFEST_KEYS = {
    "schema",
    "owner",
    "service_management_contract",
    "runtime_revision",
    "git_tree",
    "requirements_lock_sha256",
    "entrypoint",
    "python",
    "mcp_package",
    "files",
    "payload_sha256",
}
LEGACY_MANIFEST_KEYS = MANIFEST_KEYS - {"service_management_contract"}
ENTRY_KEYS = {"path", "size", "sha256"}
LIFECYCLE_CONTRACT = "user-launchagent-v1"
PAYLOAD_CONTRACT = "external-python-v1"
SUBSTRATE_SCHEMA = 3
SUBSTRATE_MANIFEST_KEYS = {
    "schema",
    "owner",
    "lifecycle_contract",
    "payload_contract",
    "runtime_revision",
    "git_tree",
    "requirements_lock_sha256",
    "entrypoint",
    "python",
    "required_python_major_minor",
    "expected_public_tool_count",
    "expected_public_surface_sha256",
    "files",
    "substrate_sha256",
}
PAYLOAD_SCHEMA = 1
PAYLOAD_MANIFEST_NAME = "payload-manifest.json"
PAYLOAD_ENTRY_KEYS = {"path", "mode", "size", "sha256"}
PAYLOAD_MANIFEST_KEYS = {
    "schema",
    "owner",
    "content_closure",
    "source_revision",
    "source_tree",
    "requirements_lock_sha256",
    "required_python_major_minor",
    "lifecycle_contract",
    "payload_contract",
    "expected_public_tool_count",
    "expected_public_surface_sha256",
    "files",
}
ZERO_COST_CANDIDATE_SCHEMA = 3
ZERO_COST_CANDIDATE_KEYS = {
    "schema",
    "bundle_identifier",
    "source_revision",
    "source_tree",
    "requirements_lock_sha256",
    "signing_mode",
    "team_identifier",
    "responsible_code",
    "lifecycle_contract",
    "payload_contract",
    "required_python_major_minor",
    "expected_public_tool_count",
    "expected_public_surface_sha256",
    "initial_payload_closure",
    "substrate_sha256",
    "record_count",
    "candidate_sha256",
}
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


def _payload_closure(entries: list[dict[str, object]]) -> str:
    canonical = "".join(
        f"{entry['path']}\\t{entry['mode']}\\t{entry['size']}\\t{entry['sha256']}\\n"
        for entry in entries
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _payload_source_inventory(source_package: Path) -> list[dict[str, object]]:
    if source_package.is_symlink() or not source_package.is_dir():
        raise PackageProvenanceError("payload source package must be a regular non-symlink directory")
    entries: list[dict[str, object]] = []
    names: set[str] = set()
    for candidate in source_package.iterdir():
        if candidate.is_symlink():
            raise PackageProvenanceError("payload source must not contain symlinks")
        if not candidate.is_file() or candidate.suffix != ".py" or candidate.name in {".", ".."}:
            raise PackageProvenanceError("payload source contains an extra or unsupported entry")
        info = candidate.stat()
        if stat.S_IMODE(info.st_mode) & 0o111:
            raise PackageProvenanceError("payload source Python files must not be executable")
        if candidate.name in names:
            raise PackageProvenanceError("payload source contains duplicate paths")
        names.add(candidate.name)
        entries.append(
            {
                "path": f"agent_runtime/{candidate.name}",
                "mode": "0444",
                "size": info.st_size,
                "sha256": _sha256(candidate),
            }
        )
    if not {"__init__.py", "server.py"}.issubset(names):
        raise PackageProvenanceError("payload source package is incomplete")
    entries.sort(key=lambda entry: str(entry["path"]).encode("utf-8"))
    return entries


def _payload_manifest_data(
    entries: list[dict[str, object]],
    *,
    revision: str,
    tree: str,
    requirements_lock_sha256: str,
    python_major_minor: str,
    public_tool_count: int,
    public_surface_sha256: str,
) -> dict[str, object]:
    _validate_identity(revision, HEX40, "payload source revision")
    _validate_identity(tree, HEX40, "payload source tree")
    _validate_identity(requirements_lock_sha256, HEX64, "payload requirements.lock")
    _validate_identity(public_surface_sha256, HEX64, "payload public surface")
    if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", python_major_minor) is None:
        raise PackageProvenanceError("payload required Python major/minor is invalid")
    if type(public_tool_count) is not int or public_tool_count < 1:
        raise PackageProvenanceError("payload expected public tool count is invalid")
    return {
        "schema": PAYLOAD_SCHEMA,
        "owner": OWNER,
        "content_closure": _payload_closure(entries),
        "source_revision": revision,
        "source_tree": tree,
        "requirements_lock_sha256": requirements_lock_sha256,
        "required_python_major_minor": python_major_minor,
        "lifecycle_contract": LIFECYCLE_CONTRACT,
        "payload_contract": PAYLOAD_CONTRACT,
        "expected_public_tool_count": public_tool_count,
        "expected_public_surface_sha256": public_surface_sha256,
        "files": entries,
    }


def _load_payload_manifest(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PackageProvenanceError("payload manifest must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageProvenanceError("payload manifest is malformed") from exc
    if not isinstance(value, dict) or set(value) != PAYLOAD_MANIFEST_KEYS:
        raise PackageProvenanceError("payload manifest shape is invalid")
    return value


def validate_payload_release(
    release: Path,
    *,
    expected_closure: str,
    expected_requirements_lock_sha256: str,
    expected_python_major_minor: str,
    expected_public_tool_count: int,
    expected_public_surface_sha256: str,
    require_directory_name: bool = True,
) -> dict[str, object]:
    closure = _validate_identity(expected_closure, HEX64, "expected payload closure")
    lock_sha = _validate_identity(
        expected_requirements_lock_sha256, HEX64, "expected payload requirements.lock"
    )
    surface_sha = _validate_identity(
        expected_public_surface_sha256, HEX64, "expected payload public surface"
    )
    if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", expected_python_major_minor) is None:
        raise PackageProvenanceError("expected payload Python major/minor is invalid")
    if type(expected_public_tool_count) is not int or expected_public_tool_count < 1:
        raise PackageProvenanceError("expected payload public tool count is invalid")
    if release.is_symlink() or not release.is_dir():
        raise PackageProvenanceError("payload release must be a regular non-symlink directory")
    if require_directory_name and release.name != closure:
        raise PackageProvenanceError("payload release directory name does not match closure")
    if stat.S_IMODE(release.stat().st_mode) != 0o555:
        raise PackageProvenanceError("payload release directory mode is invalid")

    manifest_path = release / PAYLOAD_MANIFEST_NAME
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or stat.S_IMODE(manifest_path.stat().st_mode) != 0o444
    ):
        raise PackageProvenanceError("payload manifest mode is invalid")
    manifest = _load_payload_manifest(manifest_path)
    if manifest["schema"] != PAYLOAD_SCHEMA or manifest["owner"] != OWNER:
        raise PackageProvenanceError("payload manifest ownership/schema is invalid")
    if manifest["lifecycle_contract"] != LIFECYCLE_CONTRACT or manifest["payload_contract"] != PAYLOAD_CONTRACT:
        raise PackageProvenanceError("payload substrate contract is incompatible")
    if manifest["content_closure"] != closure:
        raise PackageProvenanceError("payload manifest closure does not match selected release")
    if manifest["requirements_lock_sha256"] != lock_sha:
        raise PackageProvenanceError("payload requirements.lock identity is incompatible")
    if manifest["required_python_major_minor"] != expected_python_major_minor:
        raise PackageProvenanceError("payload Python contract is incompatible")
    if manifest["expected_public_tool_count"] != expected_public_tool_count:
        raise PackageProvenanceError("payload public tool count is incompatible")
    if manifest["expected_public_surface_sha256"] != surface_sha:
        raise PackageProvenanceError("payload public surface identity is incompatible")
    _validate_identity(manifest["source_revision"], HEX40, "payload source revision")
    _validate_identity(manifest["source_tree"], HEX40, "payload source tree")

    package = release / "agent_runtime"
    if package.is_symlink() or not package.is_dir() or stat.S_IMODE(package.stat().st_mode) != 0o555:
        raise PackageProvenanceError("payload package directory is missing, unsafe, or mutated")
    root_names = {item.name for item in release.iterdir()}
    if root_names != {PAYLOAD_MANIFEST_NAME, "agent_runtime"}:
        raise PackageProvenanceError("payload release contains extra files")

    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise PackageProvenanceError("payload manifest inventory is invalid")
    normalized: list[dict[str, object]] = []
    previous: str | None = None
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != PAYLOAD_ENTRY_KEYS:
            raise PackageProvenanceError("payload manifest inventory entry is malformed")
        path = entry["path"]
        mode = entry["mode"]
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(path, str) or re.fullmatch(r"agent_runtime/[^/]+\.py", path) is None:
            raise PackageProvenanceError("payload manifest path is invalid")
        if path in seen or (previous is not None and path <= previous):
            raise PackageProvenanceError("payload manifest inventory is duplicate or unsorted")
        if mode != "0444" or type(size) is not int or size < 0:
            raise PackageProvenanceError("payload manifest mode or size is invalid")
        _validate_identity(digest, HEX64, "payload file hash")
        target = release / path
        if target.is_symlink() or not target.is_file():
            raise PackageProvenanceError("payload inventory member is missing or unsafe")
        info = target.stat()
        if stat.S_IMODE(info.st_mode) != 0o444 or info.st_size != size or _sha256(target) != digest:
            raise PackageProvenanceError("payload inventory member was mutated")
        seen.add(path)
        previous = path
        normalized.append({"path": path, "mode": mode, "size": size, "sha256": digest})
    actual_names = {f"agent_runtime/{item.name}" for item in package.iterdir()}
    if actual_names != seen:
        raise PackageProvenanceError("payload release inventory does not close over package contents")
    actual_closure = _payload_closure(normalized)
    if actual_closure != closure:
        raise PackageProvenanceError("payload content closure mismatch")
    return manifest


def publish_payload_release(
    source_package: Path,
    releases_root: Path,
    *,
    revision: str,
    tree: str,
    requirements_lock_sha256: str,
    python_major_minor: str,
    public_tool_count: int,
    public_surface_sha256: str,
) -> dict[str, object]:
    entries = _payload_source_inventory(source_package)
    manifest = _payload_manifest_data(
        entries,
        revision=revision,
        tree=tree,
        requirements_lock_sha256=requirements_lock_sha256,
        python_major_minor=python_major_minor,
        public_tool_count=public_tool_count,
        public_surface_sha256=public_surface_sha256,
    )
    closure = str(manifest["content_closure"])
    if releases_root.is_symlink() or (releases_root.exists() and not releases_root.is_dir()):
        raise PackageProvenanceError("payload releases root is unsafe")
    releases_root.mkdir(parents=True, exist_ok=True)
    final = releases_root / closure
    if final.exists() or final.is_symlink():
        validated = validate_payload_release(
            final,
            expected_closure=closure,
            expected_requirements_lock_sha256=requirements_lock_sha256,
            expected_python_major_minor=python_major_minor,
            expected_public_tool_count=public_tool_count,
            expected_public_surface_sha256=public_surface_sha256,
        )
        if validated != manifest:
            raise PackageProvenanceError("existing payload release does not match requested publication")
        return {"content_closure": closure, "release_path": str(final), "manifest": validated}

    staging = releases_root / f".{closure}.stage-{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        raise PackageProvenanceError("payload staging path already exists")
    try:
        package = staging / "agent_runtime"
        package.mkdir(parents=True, mode=0o755)
        for entry in entries:
            name = Path(str(entry["path"])).name
            source = source_package / name
            target = package / name
            target.write_bytes(source.read_bytes())
            target.chmod(0o444)
        manifest_path = staging / PAYLOAD_MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        manifest_path.chmod(0o444)
        package.chmod(0o555)
        staging.chmod(0o555)
        validate_payload_release(
            staging,
            expected_closure=closure,
            expected_requirements_lock_sha256=requirements_lock_sha256,
            expected_python_major_minor=python_major_minor,
            expected_public_tool_count=public_tool_count,
            expected_public_surface_sha256=public_surface_sha256,
            require_directory_name=False,
        )
        try:
            _rename_candidate_no_replace(staging, final)
        except FileExistsError as exc:
            raise PackageProvenanceError("payload release already exists") from exc
        validate_payload_release(
            final,
            expected_closure=closure,
            expected_requirements_lock_sha256=requirements_lock_sha256,
            expected_python_major_minor=python_major_minor,
            expected_public_tool_count=public_tool_count,
            expected_public_surface_sha256=public_surface_sha256,
        )
    finally:
        if staging.exists() and not staging.is_symlink():
            for current, dirs, files in os.walk(staging, topdown=False):
                root = Path(current)
                for name in files:
                    (root / name).chmod(0o600)
                for name in dirs:
                    (root / name).chmod(0o700)
                root.chmod(0o700)
            shutil.rmtree(staging)
    return {"content_closure": closure, "release_path": str(final), "manifest": manifest}


def write_substrate_manifest(
    runtime: Path,
    manifest_path: Path,
    revision: str,
    tree: str,
    lock_sha: str,
    *,
    python_major_minor: str,
    public_tool_count: int,
    public_surface_sha256: str,
) -> dict[str, object]:
    _validate_identity(revision, HEX40, "substrate revision")
    _validate_identity(tree, HEX40, "substrate tree")
    _validate_identity(lock_sha, HEX64, "substrate requirements.lock")
    _validate_identity(public_surface_sha256, HEX64, "substrate public surface")
    if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", python_major_minor) is None:
        raise PackageProvenanceError("substrate required Python major/minor is invalid")
    if type(public_tool_count) is not int or public_tool_count < 1:
        raise PackageProvenanceError("substrate expected public tool count is invalid")
    first_party = runtime / "agent_runtime"
    if first_party.exists() or first_party.is_symlink():
        raise PackageProvenanceError("immutable substrate must not contain first-party agent_runtime source")
    entries = _runtime_entries(runtime)
    paths = {str(entry["path"]) for entry in entries}
    if "start.sh" not in paths or ".venv/bin/python" not in paths:
        raise PackageProvenanceError("immutable substrate execution surface is incomplete")
    data: dict[str, object] = {
        "schema": SUBSTRATE_SCHEMA,
        "owner": OWNER,
        "lifecycle_contract": LIFECYCLE_CONTRACT,
        "payload_contract": PAYLOAD_CONTRACT,
        "runtime_revision": revision,
        "git_tree": tree,
        "requirements_lock_sha256": lock_sha,
        "entrypoint": "runtime/start.sh",
        "python": "runtime/.venv/bin/python",
        "required_python_major_minor": python_major_minor,
        "expected_public_tool_count": public_tool_count,
        "expected_public_surface_sha256": public_surface_sha256,
        "files": entries,
        "substrate_sha256": aggregate_digest(entries),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return data


def _load_substrate_manifest(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PackageProvenanceError("substrate manifest must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageProvenanceError("substrate manifest is malformed") from exc
    if not isinstance(value, dict) or set(value) != SUBSTRATE_MANIFEST_KEYS:
        raise PackageProvenanceError("substrate manifest shape is invalid")
    if value.get("schema") != SUBSTRATE_SCHEMA or value.get("owner") != OWNER:
        raise PackageProvenanceError("substrate manifest ownership/schema is invalid")
    return value


def validate_substrate_manifest(
    runtime: Path,
    manifest_path: Path,
    expected_revision: str,
    expected_tree: str,
    expected_lock_sha: str,
) -> dict[str, object]:
    _validate_identity(expected_revision, HEX40, "expected substrate revision")
    _validate_identity(expected_tree, HEX40, "expected substrate tree")
    _validate_identity(expected_lock_sha, HEX64, "expected substrate requirements.lock")
    manifest = _load_substrate_manifest(manifest_path)
    if manifest["lifecycle_contract"] != LIFECYCLE_CONTRACT or manifest["payload_contract"] != PAYLOAD_CONTRACT:
        raise PackageProvenanceError("substrate lifecycle/payload contract is incompatible")
    if manifest["entrypoint"] != "runtime/start.sh" or manifest["python"] != "runtime/.venv/bin/python":
        raise PackageProvenanceError("substrate execution paths are invalid")
    if manifest["runtime_revision"] != expected_revision or manifest["git_tree"] != expected_tree:
        raise PackageProvenanceError("substrate source identity mismatch")
    if manifest["requirements_lock_sha256"] != expected_lock_sha:
        raise PackageProvenanceError("substrate requirements.lock identity mismatch")
    python_major_minor = manifest["required_python_major_minor"]
    if not isinstance(python_major_minor, str) or re.fullmatch(r"[1-9][0-9]*\.[0-9]+", python_major_minor) is None:
        raise PackageProvenanceError("substrate Python contract is invalid")
    if type(manifest["expected_public_tool_count"]) is not int or manifest["expected_public_tool_count"] < 1:
        raise PackageProvenanceError("substrate public tool count is invalid")
    _validate_identity(manifest["expected_public_surface_sha256"], HEX64, "substrate public surface")
    files = manifest["files"]
    if not isinstance(files, list):
        raise PackageProvenanceError("substrate manifest files must be a list")
    normalized: list[dict[str, object]] = []
    previous: str | None = None
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise PackageProvenanceError("substrate manifest file entry is malformed")
        path = entry["path"]
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(path, str) or not path or path.startswith("agent_runtime/"):
            raise PackageProvenanceError("substrate manifest contains first-party agent_runtime source")
        pure = PurePosixPath(path)
        if pure.is_absolute() or path != pure.as_posix() or ".." in pure.parts or "." in pure.parts:
            raise PackageProvenanceError("substrate manifest file path is invalid")
        if previous is not None and path <= previous:
            raise PackageProvenanceError("substrate manifest file entries are not strictly sorted")
        if type(size) is not int or size < 0:
            raise PackageProvenanceError("substrate manifest file size is invalid")
        _validate_identity(digest, HEX64, "substrate file hash")
        previous = path
        normalized.append({"path": path, "size": size, "sha256": digest})
    actual = _runtime_entries(runtime)
    if normalized != actual:
        raise PackageProvenanceError("substrate manifest does not exactly close over immutable Runtime bytes")
    expected_substrate_sha = _validate_identity(manifest["substrate_sha256"], HEX64, "substrate closure")
    if aggregate_digest(normalized) != expected_substrate_sha:
        raise PackageProvenanceError("substrate closure mismatch")
    if any(str(entry["path"]).startswith("agent_runtime/") for entry in actual):
        raise PackageProvenanceError("immutable substrate contains first-party agent_runtime source")
    return manifest


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
        "service_management_contract": SERVICE_MANAGEMENT_CONTRACT,
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
    if not isinstance(value, dict):
        raise PackageProvenanceError("runtime manifest shape is invalid")
    schema = value.get("schema")
    if schema == SCHEMA:
        if set(value) != MANIFEST_KEYS:
            raise PackageProvenanceError("runtime manifest shape is invalid")
        contract = value.get("service_management_contract")
        if not isinstance(contract, str) or contract not in SERVICE_MANAGEMENT_CONTRACTS:
            raise PackageProvenanceError("runtime manifest service-management contract is invalid")
    elif schema == LEGACY_SCHEMA:
        if set(value) != LEGACY_MANIFEST_KEYS:
            raise PackageProvenanceError("runtime manifest shape is invalid")
    else:
        raise PackageProvenanceError("runtime manifest schema is invalid")
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
SCREEN_CAPTURE_EXECUTABLE_RELATIVE = "Contents/MacOS/AgentRuntimeScreenCapture"
RUNTIME_PYTHON_EXECUTABLE_RELATIVE = "Contents/Resources/runtime/.venv/bin/python"
ZERO_COST_CODE_IDENTIFIERS = {
    MAIN_EXECUTABLE_RELATIVE: OWNER,
    RUNTIME_SERVICE_EXECUTABLE_RELATIVE: OWNER + ".runtime-service",
    SCREEN_CAPTURE_EXECUTABLE_RELATIVE: OWNER + ".screen-capture",
    RUNTIME_PYTHON_EXECUTABLE_RELATIVE: OWNER + ".python",
}
CURRENT_RUNTIME_SERVICE_PLIST_RELATIVE = (
    "Contents/Library/LaunchAgents/com.picmao.agent-runtime-runtime-service.plist"
)


def validate_zero_cost_lifecycle_structure(app: Path) -> dict[str, object]:
    if app.is_symlink() or not app.is_dir():
        raise PackageProvenanceError("zero-cost app must be a regular non-symlink directory")
    embedded_runtime_service = app / CURRENT_RUNTIME_SERVICE_PLIST_RELATIVE
    if embedded_runtime_service.exists() or embedded_runtime_service.is_symlink():
        raise PackageProvenanceError(
            "zero-cost lifecycle contradicts user-launchagent-v1 with embedded Runtime ServiceManagement LaunchAgent"
        )
    launchagents = app / "Contents" / "Library" / "LaunchAgents"
    if launchagents.exists() or launchagents.is_symlink():
        if launchagents.is_symlink() or not launchagents.is_dir():
            raise PackageProvenanceError("zero-cost ServiceManagement lifecycle directory is unsafe")
        try:
            if any(launchagents.iterdir()):
                raise PackageProvenanceError(
                    "zero-cost lifecycle contains unexpected embedded ServiceManagement LaunchAgent material"
                )
        except OSError as exc:
            raise PackageProvenanceError(
                "zero-cost ServiceManagement lifecycle directory cannot be inspected"
            ) from exc
    return {
        "lifecycle_contract": LIFECYCLE_CONTRACT,
        "runtime_service_management_embedded": False,
    }


def _codesign_metadata(path: Path) -> dict[str, object]:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", "-r-", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise PackageProvenanceError(f"could not inspect code-signing identity for {path.name}")
    detail = result.stdout + "\n" + result.stderr
    identifier_match = re.search(r"(?m)^Identifier=(.+)$", detail)
    if identifier_match is None or not identifier_match.group(1).strip():
        raise PackageProvenanceError(f"code-signing identifier is missing for {path.name}")
    team_match = re.search(r"(?m)^TeamIdentifier=(.+)$", detail)
    team: str | None = None
    if team_match is not None:
        candidate = team_match.group(1).strip()
        if candidate and candidate.lower() not in {"not set", "none", "null"}:
            team = candidate
    requirement_match = re.search(r"(?m)^(designated => .+)$", detail)
    if requirement_match is None:
        raise PackageProvenanceError(f"designated requirement is missing for {path.name}")
    return {
        "identifier": identifier_match.group(1).strip(),
        "team_identifier": team,
        "designated_requirement": requirement_match.group(1).strip(),
    }


def zero_cost_responsible_code_identity(app: Path, identity_reader=None) -> dict[str, object]:
    info_path = app / "Contents" / "Info.plist"
    if info_path.is_symlink() or not info_path.is_file():
        raise PackageProvenanceError("candidate Info.plist must be a regular non-symlink file")
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise PackageProvenanceError("candidate Info.plist is malformed") from exc
    if info.get("CFBundleIdentifier") != OWNER or info.get("CFBundleExecutable") != "AgentRuntimeMenuBar":
        raise PackageProvenanceError("candidate main app identity is not owned by agent-runtime")
    reader = identity_reader or _codesign_metadata
    responsible: dict[str, dict[str, str]] = {}
    for relative, expected_identifier in ZERO_COST_CODE_IDENTIFIERS.items():
        target = app / relative
        if target.is_symlink() or not target.is_file() or not os.access(target, os.X_OK):
            raise PackageProvenanceError(f"package executable is missing or unsafe: {target.name}")
        value = reader(target)
        if not isinstance(value, dict):
            raise PackageProvenanceError("code-signing identity metadata is malformed")
        identifier = value.get("identifier")
        team = value.get("team_identifier")
        requirement = value.get("designated_requirement")
        if team is not None:
            raise PackageProvenanceError("zero-cost candidate executable unexpectedly has a TeamIdentifier")
        if identifier != expected_identifier:
            raise PackageProvenanceError("zero-cost candidate executable identifier mismatch")
        expected_requirement = f'designated => identifier "{expected_identifier}"'
        if requirement != expected_requirement:
            raise PackageProvenanceError("zero-cost candidate designated requirement mismatch")
        responsible[relative] = {
            "identifier": expected_identifier,
            "designated_requirement": expected_requirement,
        }
    return {
        "signing_mode": "adhoc",
        "team_identifier": None,
        "responsible_code": responsible,
    }


def zero_cost_candidate_handoff_data(
    app: Path,
    *,
    initial_payload_closure: str,
    identity_reader=None,
) -> dict[str, object]:
    payload_closure = _validate_identity(initial_payload_closure, HEX64, "initial payload closure")
    validate_zero_cost_lifecycle_structure(app)
    manifest_path = app / "Contents" / "Resources" / "runtime-manifest.json"
    runtime = app / "Contents" / "Resources" / "runtime"
    manifest = _load_substrate_manifest(manifest_path)
    revision = _validate_identity(manifest.get("runtime_revision"), HEX40, "substrate revision")
    tree = _validate_identity(manifest.get("git_tree"), HEX40, "substrate tree")
    lock_sha = _validate_identity(manifest.get("requirements_lock_sha256"), HEX64, "substrate requirements.lock")
    validate_substrate_manifest(runtime, manifest_path, revision, tree, lock_sha)
    code_identity = zero_cost_responsible_code_identity(app, identity_reader=identity_reader)
    closure = candidate_closure(app)
    return {
        "schema": ZERO_COST_CANDIDATE_SCHEMA,
        "bundle_identifier": OWNER,
        "source_revision": revision,
        "source_tree": tree,
        "requirements_lock_sha256": lock_sha,
        **code_identity,
        "lifecycle_contract": LIFECYCLE_CONTRACT,
        "payload_contract": PAYLOAD_CONTRACT,
        "required_python_major_minor": manifest["required_python_major_minor"],
        "expected_public_tool_count": manifest["expected_public_tool_count"],
        "expected_public_surface_sha256": manifest["expected_public_surface_sha256"],
        "initial_payload_closure": payload_closure,
        "substrate_sha256": closure["candidate_sha256"],
        **closure,
    }


def _load_zero_cost_candidate_handoff(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PackageProvenanceError("zero-cost candidate handoff must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageProvenanceError("zero-cost candidate handoff is malformed") from exc
    if not isinstance(value, dict) or set(value) != ZERO_COST_CANDIDATE_KEYS:
        raise PackageProvenanceError("zero-cost candidate handoff shape is invalid")
    if value.get("schema") != ZERO_COST_CANDIDATE_SCHEMA or value.get("bundle_identifier") != OWNER:
        raise PackageProvenanceError("zero-cost candidate handoff ownership/schema is invalid")
    if value.get("signing_mode") != "adhoc" or value.get("team_identifier") is not None:
        raise PackageProvenanceError("zero-cost candidate signing authority is invalid")
    if value.get("lifecycle_contract") != LIFECYCLE_CONTRACT or value.get("payload_contract") != PAYLOAD_CONTRACT:
        raise PackageProvenanceError("zero-cost candidate lifecycle/payload contract is invalid")
    _validate_identity(value.get("source_revision"), HEX40, "zero-cost candidate revision")
    _validate_identity(value.get("source_tree"), HEX40, "zero-cost candidate tree")
    _validate_identity(value.get("requirements_lock_sha256"), HEX64, "zero-cost candidate requirements.lock")
    _validate_identity(value.get("expected_public_surface_sha256"), HEX64, "zero-cost candidate public surface")
    _validate_identity(value.get("initial_payload_closure"), HEX64, "zero-cost candidate initial payload")
    substrate_sha = _validate_identity(value.get("substrate_sha256"), HEX64, "zero-cost substrate closure")
    candidate_sha = _validate_identity(value.get("candidate_sha256"), HEX64, "zero-cost candidate closure")
    if substrate_sha != candidate_sha:
        raise PackageProvenanceError("zero-cost candidate substrate closure is inconsistent")
    if type(value.get("expected_public_tool_count")) is not int or int(value["expected_public_tool_count"]) < 1:
        raise PackageProvenanceError("zero-cost candidate public tool count is invalid")
    python_version = value.get("required_python_major_minor")
    if not isinstance(python_version, str) or re.fullmatch(r"[1-9][0-9]*\.[0-9]+", python_version) is None:
        raise PackageProvenanceError("zero-cost candidate Python contract is invalid")
    if type(value.get("record_count")) is not int or int(value["record_count"]) < 1:
        raise PackageProvenanceError("zero-cost candidate record count is invalid")
    responsible = value.get("responsible_code")
    if not isinstance(responsible, dict) or set(responsible) != set(ZERO_COST_CODE_IDENTIFIERS):
        raise PackageProvenanceError("zero-cost candidate responsible-code inventory is invalid")
    for relative, expected_identifier in ZERO_COST_CODE_IDENTIFIERS.items():
        expected_requirement = f'designated => identifier "{expected_identifier}"'
        if responsible.get(relative) != {
            "identifier": expected_identifier,
            "designated_requirement": expected_requirement,
        }:
            raise PackageProvenanceError("zero-cost candidate responsible-code identity is invalid")
    return value


def validate_zero_cost_candidate(
    app: Path,
    handoff_path: Path,
    payload_release: Path,
    *,
    identity_reader=None,
) -> dict[str, object]:
    expected = _load_zero_cost_candidate_handoff(handoff_path)
    payload_manifest = validate_payload_release(
        payload_release,
        expected_closure=str(expected["initial_payload_closure"]),
        expected_requirements_lock_sha256=str(expected["requirements_lock_sha256"]),
        expected_python_major_minor=str(expected["required_python_major_minor"]),
        expected_public_tool_count=int(expected["expected_public_tool_count"]),
        expected_public_surface_sha256=str(expected["expected_public_surface_sha256"]),
    )
    if payload_manifest["source_revision"] != expected["source_revision"] or payload_manifest["source_tree"] != expected["source_tree"]:
        raise PackageProvenanceError("initial payload source identity does not match substrate source")
    _verify_codesign(app)
    actual = zero_cost_candidate_handoff_data(
        app,
        initial_payload_closure=str(expected["initial_payload_closure"]),
        identity_reader=identity_reader,
    )
    if actual != expected:
        raise PackageProvenanceError("zero-cost candidate identity does not match external handoff")
    _verify_codesign(app)
    final = zero_cost_candidate_handoff_data(
        app,
        initial_payload_closure=str(expected["initial_payload_closure"]),
        identity_reader=identity_reader,
    )
    if final != expected:
        raise PackageProvenanceError("zero-cost candidate changed during validation")
    return expected


def seal_zero_cost_candidate(
    app: Path,
    handoff_path: Path,
    payload_release: Path,
    *,
    identity_reader=None,
) -> dict[str, object]:
    app_real = app.resolve()
    handoff_real = handoff_path.resolve(strict=False)
    if handoff_real == app_real or app_real in handoff_real.parents:
        raise PackageProvenanceError("candidate handoff must remain external to the app bundle")
    payload_manifest = _load_payload_manifest(payload_release / PAYLOAD_MANIFEST_NAME)
    payload_closure = _validate_identity(payload_manifest.get("content_closure"), HEX64, "initial payload closure")
    validate_payload_release(
        payload_release,
        expected_closure=payload_closure,
        expected_requirements_lock_sha256=str(payload_manifest.get("requirements_lock_sha256")),
        expected_python_major_minor=str(payload_manifest.get("required_python_major_minor")),
        expected_public_tool_count=int(payload_manifest.get("expected_public_tool_count")),
        expected_public_surface_sha256=str(payload_manifest.get("expected_public_surface_sha256")),
    )
    _verify_codesign(app)
    data = zero_cost_candidate_handoff_data(
        app,
        initial_payload_closure=payload_closure,
        identity_reader=identity_reader,
    )
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = handoff_path.with_name(handoff_path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, handoff_path)
    validate_zero_cost_candidate(app, handoff_path, payload_release, identity_reader=identity_reader)
    return data


def publish_zero_cost_distribution(
    app: Path,
    handoff_path: Path,
    payload_release: Path,
    candidates_root: Path,
    *,
    identity_reader=None,
) -> dict[str, object]:
    if app.name != "Agent Runtime.app" or handoff_path.name != "Agent Runtime.candidate.json":
        raise PackageProvenanceError("zero-cost distribution paths use unexpected names")
    if app.parent != handoff_path.parent:
        raise PackageProvenanceError("zero-cost app and handoff must share one staging directory")
    staging_root = app.parent
    if payload_release.parent.name != "payloads" or payload_release.parent.parent != staging_root:
        raise PackageProvenanceError("initial payload release must be inside the distribution staging root")
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise PackageProvenanceError("zero-cost distribution staging directory is unsafe")
    candidate = validate_zero_cost_candidate(
        app,
        handoff_path,
        payload_release,
        identity_reader=identity_reader,
    )
    candidate_sha = str(candidate["candidate_sha256"])
    final_root = candidates_root / candidate_sha
    if final_root.exists() or final_root.is_symlink():
        raise PackageProvenanceError("candidate output already exists")
    candidates_root.mkdir(parents=True, exist_ok=True)
    try:
        _rename_candidate_no_replace(staging_root, final_root)
    except FileExistsError as exc:
        raise PackageProvenanceError("candidate output already exists") from exc
    except OSError as exc:
        raise PackageProvenanceError("candidate publication failed: " + str(exc)) from exc
    final_payload = final_root / "payloads" / str(candidate["initial_payload_closure"])
    return {
        **candidate,
        "candidate_app": str(final_root / app.name),
        "candidate_handoff": str(final_root / handoff_path.name),
        "initial_payload_release": str(final_payload),
    }


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
    screen_capture = app / SCREEN_CAPTURE_EXECUTABLE_RELATIVE
    for target in (main, runtime_service, screen_capture):
        if target.is_symlink() or not target.is_file() or not os.access(target, os.X_OK):
            raise PackageProvenanceError(f"package executable is missing or unsafe: {target.name}")
    reader = team_identifier_reader or _codesign_team_identifier
    main_team = reader(main)
    runtime_team = reader(runtime_service)
    screen_capture_team = reader(screen_capture)
    if not isinstance(main_team, str) or not main_team.strip():
        raise PackageProvenanceError("main app TeamIdentifier is missing")
    if not isinstance(runtime_team, str) or not runtime_team.strip():
        raise PackageProvenanceError("Runtime responsible executable TeamIdentifier is missing")
    if not isinstance(screen_capture_team, str) or not screen_capture_team.strip():
        raise PackageProvenanceError("screen capture helper TeamIdentifier is missing")
    if main_team != runtime_team or main_team != screen_capture_team:
        raise PackageProvenanceError("package executable TeamIdentifier does not match main app TeamIdentifier")
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


def _validate_external_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise PackageProvenanceError(f"{label} must be an exact lowercase 64-hex SHA-256")
    return value


def _candidate_handoff_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise PackageProvenanceError("candidate handoff must be a regular non-symlink file")
    return _sha256(path)


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


def validate_pinned_candidate(
    app: Path,
    handoff_path: Path,
    expected_candidate_sha256: str,
    expected_handoff_sha256: str,
) -> dict[str, object]:
    candidate_sha = _validate_external_sha256(expected_candidate_sha256, "expected candidate SHA-256")
    handoff_sha = _validate_external_sha256(expected_handoff_sha256, "expected handoff SHA-256")

    handoff_before = _candidate_handoff_sha256(handoff_path)
    if handoff_before != handoff_sha:
        raise PackageProvenanceError("candidate handoff SHA-256 does not match external authority")
    expected = _load_candidate_handoff(handoff_path)
    if expected["candidate_sha256"] != candidate_sha:
        raise PackageProvenanceError("candidate handoff closure does not match external candidate authority")

    closure_before = candidate_closure(app)
    if (
        closure_before["candidate_sha256"] != candidate_sha
        or closure_before["record_count"] != expected["record_count"]
    ):
        raise PackageProvenanceError("candidate closure does not match external authority")

    actual = _candidate_handoff_data(app)
    if actual != expected:
        raise PackageProvenanceError("candidate identity does not match exact external handoff")

    closure_after = candidate_closure(app)
    if closure_after != closure_before or closure_after["candidate_sha256"] != candidate_sha:
        raise PackageProvenanceError("candidate changed during pinned validation")
    handoff_after = _candidate_handoff_sha256(handoff_path)
    if handoff_after != handoff_before or handoff_after != handoff_sha:
        raise PackageProvenanceError("candidate handoff changed during pinned validation")
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


RENAME_EXCL = 0x00000004


def _rename_candidate_no_replace(source: Path, destination: Path) -> None:
    try:
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
    except AttributeError as exc:
        raise PackageProvenanceError("atomic no-replace publication is unavailable") from exc
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renamex_np(os.fsencode(source), os.fsencode(destination), RENAME_EXCL)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(destination))
    raise OSError(error, os.strerror(error), str(destination))


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
        _rename_candidate_no_replace(staging_root, final_root)
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
    substrate_manifest = subparsers.add_parser("substrate-manifest")
    substrate_manifest.add_argument("runtime", type=Path)
    substrate_manifest.add_argument("manifest_path", type=Path)
    substrate_manifest.add_argument("revision")
    substrate_manifest.add_argument("tree")
    substrate_manifest.add_argument("lock", type=Path)
    substrate_manifest.add_argument("python_major_minor")
    substrate_manifest.add_argument("public_tool_count", type=int)
    substrate_manifest.add_argument("public_surface_sha256")
    publish_payload = subparsers.add_parser("publish-payload")
    publish_payload.add_argument("source_package", type=Path)
    publish_payload.add_argument("releases_root", type=Path)
    publish_payload.add_argument("revision")
    publish_payload.add_argument("tree")
    publish_payload.add_argument("lock", type=Path)
    publish_payload.add_argument("python_major_minor")
    publish_payload.add_argument("public_tool_count", type=int)
    publish_payload.add_argument("public_surface_sha256")
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
    zero_cost_seal = subparsers.add_parser("seal-zero-cost")
    zero_cost_seal.add_argument("app", type=Path)
    zero_cost_seal.add_argument("handoff", type=Path)
    zero_cost_seal.add_argument("payload_release", type=Path)
    zero_cost_publish = subparsers.add_parser("publish-zero-cost")
    zero_cost_publish.add_argument("app", type=Path)
    zero_cost_publish.add_argument("handoff", type=Path)
    zero_cost_publish.add_argument("payload_release", type=Path)
    zero_cost_publish.add_argument("candidates_root", type=Path)
    zero_cost_validate = subparsers.add_parser("validate-zero-cost")
    zero_cost_validate.add_argument("app", type=Path)
    zero_cost_validate.add_argument("handoff", type=Path)
    zero_cost_validate.add_argument("payload_release", type=Path)
    candidate_validate = subparsers.add_parser("validate-candidate")
    candidate_validate.add_argument("app", type=Path)
    candidate_validate.add_argument("handoff", type=Path)
    pinned_validate = subparsers.add_parser("validate-pinned-candidate")
    pinned_validate.add_argument("app", type=Path)
    pinned_validate.add_argument("handoff", type=Path)
    pinned_validate.add_argument("expected_candidate_sha256")
    pinned_validate.add_argument("expected_handoff_sha256")
    args = parser.parse_args()
    try:
        if args.command == "stage":
            revision, tree = export_head(args.repo, args.destination)
            print(f"{revision}\t{tree}")
        elif args.command == "manifest":
            write_manifest(args.runtime, args.manifest_path, args.revision, args.tree, lock_sha256(args.lock))
        elif args.command == "substrate-manifest":
            write_substrate_manifest(
                args.runtime,
                args.manifest_path,
                args.revision,
                args.tree,
                lock_sha256(args.lock),
                python_major_minor=args.python_major_minor,
                public_tool_count=args.public_tool_count,
                public_surface_sha256=args.public_surface_sha256,
            )
        elif args.command == "publish-payload":
            published = publish_payload_release(
                args.source_package,
                args.releases_root,
                revision=args.revision,
                tree=args.tree,
                requirements_lock_sha256=lock_sha256(args.lock),
                python_major_minor=args.python_major_minor,
                public_tool_count=args.public_tool_count,
                public_surface_sha256=args.public_surface_sha256,
            )
            print(f"{published['content_closure']}\t{published['release_path']}")
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
        elif args.command == "seal-zero-cost":
            print(
                json.dumps(
                    seal_zero_cost_candidate(args.app, args.handoff, args.payload_release),
                    sort_keys=True,
                )
            )
        elif args.command == "publish-zero-cost":
            published = publish_zero_cost_distribution(
                args.app,
                args.handoff,
                args.payload_release,
                args.candidates_root,
            )
            print(
                f"{published['candidate_app']}\t{published['candidate_handoff']}\t"
                f"{published['initial_payload_release']}\t{published['candidate_sha256']}"
            )
        elif args.command == "validate-zero-cost":
            print(
                json.dumps(
                    validate_zero_cost_candidate(args.app, args.handoff, args.payload_release),
                    sort_keys=True,
                )
            )
        elif args.command == "validate-candidate":
            print(json.dumps(validate_candidate(args.app, args.handoff), sort_keys=True))
        else:
            print(
                json.dumps(
                    validate_pinned_candidate(
                        args.app,
                        args.handoff,
                        args.expected_candidate_sha256,
                        args.expected_handoff_sha256,
                    ),
                    sort_keys=True,
                )
            )
    except PackageProvenanceError as exc:
        print("PROVENANCE ERROR: " + str(exc), file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
