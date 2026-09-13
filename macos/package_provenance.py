#!/usr/bin/env python3
"""Immutable source staging and closed-world Runtime payload provenance."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

SCHEMA = 1
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
        bundle.extractall(destination, members=members)
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
    args = parser.parse_args()
    try:
        if args.command == "stage":
            revision, tree = export_head(args.repo, args.destination)
            print(f"{revision}\t{tree}")
        elif args.command == "manifest":
            write_manifest(args.runtime, args.manifest_path, args.revision, args.tree, lock_sha256(args.lock))
        else:
            validate_manifest(
                args.runtime,
                args.manifest_path,
                args.revision,
                args.tree,
                lock_sha256(args.lock),
            )
    except PackageProvenanceError as exc:
        print("PROVENANCE ERROR: " + str(exc), file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
