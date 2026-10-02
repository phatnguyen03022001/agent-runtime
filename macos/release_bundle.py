#!/usr/bin/env python3
"""Build and verify deterministic release assets from one frozen zero-cost candidate."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MACOS_ROOT = Path(__file__).resolve().parent
if str(MACOS_ROOT) not in sys.path:
    sys.path.insert(0, str(MACOS_ROOT))

import package_provenance as provenance

MANIFEST_SCHEMA = 1
MANIFEST_NAME = "release-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS.txt"
ROOT_NAMES = {"Agent Runtime.app", "Agent Runtime.candidate.json", "payloads"}
MANIFEST_KEYS = {
    "schema",
    "owner",
    "runtime_version",
    "source_revision",
    "source_tree",
    "candidate_sha256",
    "initial_payload_closure",
    "requirements_lock_sha256",
    "expected_public_tool_count",
    "expected_public_surface_sha256",
    "substrate_manifest_schema",
    "candidate_handoff_schema",
    "payload_schema",
    "archive_filename",
    "archive_sha256",
    "archive_size_bytes",
    "archive_member_count",
}
DENIED_COMPONENTS = {
    ".git",
    ".agent",
    ".pytest_cache",
    ".cache",
    "__pycache__",
    "runtime.env",
    "credentials",
}
GENERIC_RESIDUE_COMPONENTS = {"cache", "caches", "log", "logs"}
SITE_PACKAGES_PREFIX = (
    "Agent Runtime.app",
    "Contents",
    "Resources",
    "runtime",
    ".venv",
    "lib",
    "python3.13",
    "site-packages",
)
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")


class ReleaseBundleError(RuntimeError):
    pass


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseBundleError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseBundleError(f"{label} must be a regular non-symlink file")


def _require_empty_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseBundleError(f"{label} must already exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseBundleError(f"{label} must be a regular non-symlink directory")
    try:
        if any(path.iterdir()):
            raise ReleaseBundleError(f"{label} must be empty")
    except OSError as exc:
        raise ReleaseBundleError(f"{label} cannot be inspected") from exc


def _validated_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or any(ch in value for ch in "\x00\t\r\n\\"):
        raise ReleaseBundleError("release member path is invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ReleaseBundleError("release member path is not valid UTF-8") from exc
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value != pure.as_posix()
        or value == "."
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ReleaseBundleError("release member path is unsafe")
    return pure


def _reject_forbidden_material(relative: PurePosixPath) -> None:
    parts = relative.parts
    inside_site_packages = (
        len(parts) > len(SITE_PACKAGES_PREFIX)
        and parts[: len(SITE_PACKAGES_PREFIX)] == SITE_PACKAGES_PREFIX
    )
    for index, part in enumerate(parts):
        lowered = part.lower()
        if lowered in DENIED_COMPONENTS or lowered.startswith(".env") or lowered.endswith(".pyc"):
            raise ReleaseBundleError("release candidate contains forbidden checkout/config/cache material")
        if (
            lowered in GENERIC_RESIDUE_COMPONENTS
            and not (inside_site_packages and index >= len(SITE_PACKAGES_PREFIX))
        ):
            raise ReleaseBundleError("release candidate contains forbidden checkout/config/cache material")


def _safe_mode(info: os.stat_result, *, label: str) -> int:
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o7000:
        raise ReleaseBundleError(f"{label} has special permission bits")
    if mode & 0o022:
        raise ReleaseBundleError(f"{label} is group/world writable")
    return mode


def _operator_home_markers() -> tuple[bytes, ...]:
    values = {os.environ.get("HOME"), str(Path.home())}
    markers: list[bytes] = []
    for value in values:
        if not value or value == "/" or not os.path.isabs(value):
            continue
        try:
            marker = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ReleaseBundleError("operator HOME path is not valid UTF-8") from exc
        if marker not in markers:
            markers.append(marker)
    markers.sort()
    return tuple(markers)


def _reject_embedded_operator_home(path: Path, markers: tuple[bytes, ...]) -> None:
    if not markers:
        return
    overlap = max(len(marker) for marker in markers) - 1
    carry = b""
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                data = carry + chunk
                if any(marker in data for marker in markers):
                    raise ReleaseBundleError("release candidate embeds an operator HOME path")
                carry = data[-overlap:] if overlap > 0 else b""
    except ReleaseBundleError:
        raise
    except OSError as exc:
        raise ReleaseBundleError("release candidate file cannot be inspected") from exc


def _candidate_entries(root: Path) -> list[tuple[str, Path, int, bool, int]]:
    entries: list[tuple[str, Path, int, bool, int]] = []
    home_markers = _operator_home_markers()

    def visit(path: Path, relative: str) -> None:
        pure = _validated_relative_path(relative)
        _reject_forbidden_material(pure)
        try:
            info = path.lstat()
        except OSError as exc:
            raise ReleaseBundleError("release candidate entry cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseBundleError("release candidate must not contain symlinks")
        mode = _safe_mode(info, label=relative)
        if stat.S_ISDIR(info.st_mode):
            entries.append((relative, path, mode, True, 0))
            try:
                children = list(path.iterdir())
            except OSError as exc:
                raise ReleaseBundleError("release candidate directory cannot be inspected") from exc
            try:
                children.sort(key=lambda item: item.name.encode("utf-8", "strict"))
            except UnicodeEncodeError as exc:
                raise ReleaseBundleError("release candidate path is not valid UTF-8") from exc
            for child in children:
                visit(child, f"{relative}/{child.name}")
        elif stat.S_ISREG(info.st_mode):
            _reject_embedded_operator_home(path, home_markers)
            entries.append((relative, path, mode, False, info.st_size))
        else:
            raise ReleaseBundleError("release candidate contains a special or unsupported entry")

    try:
        top = list(root.iterdir())
    except OSError as exc:
        raise ReleaseBundleError("candidate root cannot be inspected") from exc
    try:
        top.sort(key=lambda item: item.name.encode("utf-8", "strict"))
    except UnicodeEncodeError as exc:
        raise ReleaseBundleError("candidate root path is not valid UTF-8") from exc
    if {item.name for item in top} != ROOT_NAMES:
        raise ReleaseBundleError("candidate root inventory is not exactly the zero-cost bundle")
    for item in top:
        visit(item, item.name)
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    return entries


def _load_json(path: Path, label: str) -> dict[str, object]:
    _require_regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseBundleError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise ReleaseBundleError(f"{label} shape is invalid")
    return value


def _runtime_version(app: Path) -> str:
    info_path = app / "Contents" / "Info.plist"
    _require_regular_file(info_path, "candidate Info.plist")
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ReleaseBundleError("candidate Info.plist is malformed") from exc
    value = info.get("CFBundleShortVersionString")
    if not isinstance(value, str) or VERSION.fullmatch(value) is None:
        raise ReleaseBundleError("candidate Runtime version is invalid")
    return value


def _validate_candidate_root(
    candidate_root: Path,
    *,
    require_published_name: bool,
    identity_reader=None,
) -> dict[str, object]:
    candidate_root = _absolute(candidate_root)
    try:
        info = candidate_root.lstat()
    except OSError as exc:
        raise ReleaseBundleError("candidate root is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseBundleError("candidate root must be a regular non-symlink directory")

    entries = _candidate_entries(candidate_root)
    app = candidate_root / "Agent Runtime.app"
    handoff = candidate_root / "Agent Runtime.candidate.json"
    payloads = candidate_root / "payloads"
    try:
        payload_children = list(payloads.iterdir())
    except OSError as exc:
        raise ReleaseBundleError("candidate payloads root cannot be inspected") from exc
    if len(payload_children) != 1:
        raise ReleaseBundleError("candidate must contain exactly one initial payload release")
    payload_release = payload_children[0]
    if payload_release.is_symlink() or not payload_release.is_dir():
        raise ReleaseBundleError("initial payload release is unsafe")

    candidate = provenance.validate_zero_cost_candidate(
        app,
        handoff,
        payload_release,
        identity_reader=identity_reader,
    )
    candidate_sha = str(candidate["candidate_sha256"])
    if HEX64.fullmatch(candidate_sha) is None:
        raise ReleaseBundleError("candidate SHA-256 identity is invalid")
    if require_published_name and candidate_root.name != candidate_sha:
        raise ReleaseBundleError("published candidate root name does not match candidate identity")
    if payload_release.name != candidate.get("initial_payload_closure"):
        raise ReleaseBundleError("initial payload directory does not match candidate handoff")

    substrate_manifest = _load_json(
        app / "Contents" / "Resources" / "runtime-manifest.json",
        "substrate manifest",
    )
    payload_manifest = _load_json(
        payload_release / provenance.PAYLOAD_MANIFEST_NAME,
        "payload manifest",
    )
    version = _runtime_version(app)

    source_revision = candidate.get("source_revision")
    source_tree = candidate.get("source_tree")
    lock_sha = candidate.get("requirements_lock_sha256")
    surface_sha = candidate.get("expected_public_surface_sha256")
    if not isinstance(source_revision, str) or HEX40.fullmatch(source_revision) is None:
        raise ReleaseBundleError("candidate source revision is invalid")
    if not isinstance(source_tree, str) or HEX40.fullmatch(source_tree) is None:
        raise ReleaseBundleError("candidate source tree is invalid")
    if not isinstance(lock_sha, str) or HEX64.fullmatch(lock_sha) is None:
        raise ReleaseBundleError("candidate requirements.lock identity is invalid")
    if not isinstance(surface_sha, str) or HEX64.fullmatch(surface_sha) is None:
        raise ReleaseBundleError("candidate public surface identity is invalid")
    tool_count = candidate.get("expected_public_tool_count")
    if type(tool_count) is not int or tool_count < 1:
        raise ReleaseBundleError("candidate public tool count is invalid")
    for schema_value, label in (
        (substrate_manifest.get("schema"), "substrate manifest schema"),
        (candidate.get("schema"), "candidate handoff schema"),
        (payload_manifest.get("schema"), "payload schema"),
    ):
        if type(schema_value) is not int or schema_value < 1:
            raise ReleaseBundleError(f"{label} is invalid")

    return {
        "candidate_root": candidate_root,
        "app": app,
        "handoff": handoff,
        "payload_release": payload_release,
        "entries": entries,
        "runtime_version": version,
        "source_revision": source_revision,
        "source_tree": source_tree,
        "candidate_sha256": candidate_sha,
        "initial_payload_closure": str(candidate["initial_payload_closure"]),
        "requirements_lock_sha256": lock_sha,
        "expected_public_tool_count": tool_count,
        "expected_public_surface_sha256": surface_sha,
        "substrate_manifest_schema": int(substrate_manifest["schema"]),
        "candidate_handoff_schema": int(candidate["schema"]),
        "payload_schema": int(payload_manifest["schema"]),
    }


def archive_filename(runtime_version: str, candidate_sha256: str) -> str:
    if VERSION.fullmatch(runtime_version) is None or HEX64.fullmatch(candidate_sha256) is None:
        raise ReleaseBundleError("archive identity is invalid")
    return f"agent-runtime-{runtime_version}-arm64-{candidate_sha256}.tar.gz"


def _write_archive(
    destination: Path,
    entries: list[tuple[str, Path, int, bool, int]],
) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, source, mode, is_dir, size in entries:
                    info = tarfile.TarInfo(relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    info.mode = mode
                    info.pax_headers = {}
                    if is_dir:
                        info.type = tarfile.DIRTYPE
                        info.size = 0
                        archive.addfile(info)
                    else:
                        info.type = tarfile.REGTYPE
                        info.size = size
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)


def _canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _manifest_from_candidate(
    candidate: dict[str, object],
    *,
    filename: str,
    digest: str,
    size: int,
    member_count: int,
) -> dict[str, object]:
    return {
        "schema": MANIFEST_SCHEMA,
        "owner": provenance.OWNER,
        "runtime_version": candidate["runtime_version"],
        "source_revision": candidate["source_revision"],
        "source_tree": candidate["source_tree"],
        "candidate_sha256": candidate["candidate_sha256"],
        "initial_payload_closure": candidate["initial_payload_closure"],
        "requirements_lock_sha256": candidate["requirements_lock_sha256"],
        "expected_public_tool_count": candidate["expected_public_tool_count"],
        "expected_public_surface_sha256": candidate["expected_public_surface_sha256"],
        "substrate_manifest_schema": candidate["substrate_manifest_schema"],
        "candidate_handoff_schema": candidate["candidate_handoff_schema"],
        "payload_schema": candidate["payload_schema"],
        "archive_filename": filename,
        "archive_sha256": digest,
        "archive_size_bytes": size,
        "archive_member_count": member_count,
    }


def _parse_checksums(path: Path) -> tuple[str, str]:
    _require_regular_file(path, CHECKSUMS_NAME)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseBundleError("SHA256SUMS.txt cannot be read") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\n]+\.tar\.gz)\n", text)
    if match is None:
        raise ReleaseBundleError("SHA256SUMS.txt must contain exactly one newline-terminated archive checksum")
    return match.group(1), match.group(2)


def _load_release_manifest(path: Path) -> dict[str, object]:
    value = _load_json(path, MANIFEST_NAME)
    if set(value) != MANIFEST_KEYS:
        raise ReleaseBundleError("release manifest shape is invalid")
    if value.get("schema") != MANIFEST_SCHEMA or value.get("owner") != provenance.OWNER:
        raise ReleaseBundleError("release manifest ownership/schema is invalid")
    for key in (
        "source_revision",
        "source_tree",
        "candidate_sha256",
        "initial_payload_closure",
        "requirements_lock_sha256",
        "expected_public_surface_sha256",
        "archive_sha256",
    ):
        pattern = HEX40 if key in {"source_revision", "source_tree"} else HEX64
        value_item = value.get(key)
        if not isinstance(value_item, str) or pattern.fullmatch(value_item) is None:
            raise ReleaseBundleError(f"release manifest {key} is invalid")
    if not isinstance(value.get("runtime_version"), str) or VERSION.fullmatch(str(value["runtime_version"])) is None:
        raise ReleaseBundleError("release manifest Runtime version is invalid")
    if (
        not isinstance(value.get("archive_filename"), str)
        or "/" in str(value["archive_filename"])
        or "\\" in str(value["archive_filename"])
    ):
        raise ReleaseBundleError("release manifest archive filename is invalid")
    for key in (
        "expected_public_tool_count",
        "substrate_manifest_schema",
        "candidate_handoff_schema",
        "payload_schema",
        "archive_size_bytes",
        "archive_member_count",
    ):
        item = value.get(key)
        minimum = 0 if key in {"archive_size_bytes", "archive_member_count"} else 1
        if type(item) is not int or item < minimum:
            raise ReleaseBundleError(f"release manifest {key} is invalid")
    return value


def _validated_tar_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    seen: set[str] = set()
    by_name: dict[str, tarfile.TarInfo] = {}
    roots: set[str] = set()
    for member in members:
        pure = _validated_relative_path(member.name)
        _reject_forbidden_material(pure)
        if member.name in seen:
            raise ReleaseBundleError("release archive contains a duplicate member")
        seen.add(member.name)
        by_name[member.name] = member
        roots.add(pure.parts[0])
        if not (member.isdir() or member.isreg()):
            raise ReleaseBundleError("release archive contains a symlink, hardlink, or special member")
        if member.mode & 0o7000 or member.mode & 0o022:
            raise ReleaseBundleError("release archive member mode is unsafe")
        if member.uid != 0 or member.gid != 0 or member.uname or member.gname or member.mtime != 0:
            raise ReleaseBundleError("release archive metadata is not deterministic")
        if len(pure.parts) > 1:
            parent_name = PurePosixPath(*pure.parts[:-1]).as_posix()
            parent = by_name.get(parent_name)
            if parent is None:
                # Parent may appear later in a malicious archive; require explicit directory after full scan.
                pass
    if roots != ROOT_NAMES:
        raise ReleaseBundleError("release archive root inventory is invalid")
    for required, want_dir in (
        ("Agent Runtime.app", True),
        ("Agent Runtime.candidate.json", False),
        ("payloads", True),
    ):
        member = by_name.get(required)
        if member is None or member.isdir() != want_dir or member.isreg() == want_dir:
            raise ReleaseBundleError("release archive root inventory is invalid")
    for member in members:
        pure = PurePosixPath(member.name)
        if len(pure.parts) > 1:
            parent_name = PurePosixPath(*pure.parts[:-1]).as_posix()
            parent = by_name.get(parent_name)
            if parent is None or not parent.isdir():
                raise ReleaseBundleError("release archive member parent is missing or not a directory")
    return members


def _extract_members(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    destination: Path,
) -> None:
    _require_empty_directory(destination, "extraction directory")
    directories = [member for member in members if member.isdir()]
    files = [member for member in members if member.isreg()]
    directories.sort(key=lambda item: (len(PurePosixPath(item.name).parts), item.name.encode("utf-8")))
    files.sort(key=lambda item: item.name.encode("utf-8"))

    for member in directories:
        target = destination / PurePosixPath(member.name)
        target.mkdir(mode=0o700)
    for member in files:
        target = destination / PurePosixPath(member.name)
        parent = target.parent
        if parent != destination and (parent.is_symlink() or not parent.is_dir()):
            raise ReleaseBundleError("release archive extraction parent is unsafe")
        source = archive.extractfile(member)
        if source is None:
            raise ReleaseBundleError("release archive regular member cannot be read")
        try:
            with target.open("xb") as handle:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
        finally:
            source.close()
        target.chmod(member.mode)
    for member in sorted(
        directories,
        key=lambda item: (-len(PurePosixPath(item.name).parts), item.name.encode("utf-8")),
    ):
        (destination / PurePosixPath(member.name)).chmod(member.mode)


def _compare_manifest_candidate(manifest: dict[str, object], candidate: dict[str, object]) -> None:
    for key in (
        "runtime_version",
        "source_revision",
        "source_tree",
        "candidate_sha256",
        "initial_payload_closure",
        "requirements_lock_sha256",
        "expected_public_tool_count",
        "expected_public_surface_sha256",
        "substrate_manifest_schema",
        "candidate_handoff_schema",
        "payload_schema",
    ):
        if manifest.get(key) != candidate.get(key):
            raise ReleaseBundleError(f"release manifest does not match extracted candidate: {key}")


def verify_release_bundle(
    archive_path: Path,
    manifest_path: Path,
    checksums_path: Path,
    *,
    extract_dir: Path | None = None,
    identity_reader=None,
) -> dict[str, object]:
    archive_path = _absolute(archive_path)
    manifest_path = _absolute(manifest_path)
    checksums_path = _absolute(checksums_path)
    _require_regular_file(archive_path, "release archive")
    manifest = _load_release_manifest(manifest_path)
    checksum_sha, checksum_name = _parse_checksums(checksums_path)

    actual_sha = _sha256(archive_path)
    actual_size = archive_path.stat().st_size
    if checksum_name != archive_path.name or checksum_sha != actual_sha:
        raise ReleaseBundleError("release archive checksum does not match SHA256SUMS.txt")
    if manifest["archive_filename"] != archive_path.name:
        raise ReleaseBundleError("release manifest archive filename mismatch")
    if manifest["archive_sha256"] != actual_sha or manifest["archive_size_bytes"] != actual_size:
        raise ReleaseBundleError("release manifest archive hash/size mismatch")

    temporary = None
    if extract_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-runtime-release-verify-")
        extraction_root = Path(temporary.name)
    else:
        extraction_root = _absolute(extract_dir)
        _require_empty_directory(extraction_root, "extraction directory")

    try:
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                members = _validated_tar_members(archive)
                if len(members) != manifest["archive_member_count"]:
                    raise ReleaseBundleError("release manifest archive member count mismatch")
                _extract_members(archive, members, extraction_root)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ReleaseBundleError("release archive is malformed") from exc

        candidate = _validate_candidate_root(
            extraction_root,
            require_published_name=False,
            identity_reader=identity_reader,
        )
        _compare_manifest_candidate(manifest, candidate)
        expected_name = archive_filename(
            str(candidate["runtime_version"]),
            str(candidate["candidate_sha256"]),
        )
        if archive_path.name != expected_name:
            raise ReleaseBundleError("release archive filename is not derived from Runtime/candidate identity")
        return {
            "archive_filename": archive_path.name,
            "archive_sha256": actual_sha,
            "archive_size_bytes": actual_size,
            "archive_member_count": len(members),
            "candidate_sha256": candidate["candidate_sha256"],
            "source_revision": candidate["source_revision"],
            "source_tree": candidate["source_tree"],
            "initial_payload_closure": candidate["initial_payload_closure"],
            "extracted_root": str(extraction_root) if extract_dir is not None else None,
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def build_release_bundle(
    candidate_root: Path,
    output_dir: Path,
    *,
    identity_reader=None,
) -> dict[str, object]:
    candidate_root = _absolute(candidate_root)
    output_dir = _absolute(output_dir)
    _require_empty_directory(output_dir, "output directory")
    candidate = _validate_candidate_root(
        candidate_root,
        require_published_name=True,
        identity_reader=identity_reader,
    )
    filename = archive_filename(
        str(candidate["runtime_version"]),
        str(candidate["candidate_sha256"]),
    )

    with tempfile.TemporaryDirectory(prefix="agent-runtime-release-build-") as raw:
        stage = Path(raw)
        archive_path = stage / filename
        manifest_path = stage / MANIFEST_NAME
        checksums_path = stage / CHECKSUMS_NAME
        _write_archive(archive_path, list(candidate["entries"]))
        digest = _sha256(archive_path)
        size = archive_path.stat().st_size
        manifest = _manifest_from_candidate(
            candidate,
            filename=filename,
            digest=digest,
            size=size,
            member_count=len(candidate["entries"]),
        )
        manifest_path.write_bytes(_canonical_manifest_bytes(manifest))
        checksums_path.write_text(f"{digest}  {filename}\n", encoding="utf-8")

        # Revalidate the source after archive creation, then independently verify the staged assets.
        final_candidate = _validate_candidate_root(
            candidate_root,
            require_published_name=True,
            identity_reader=identity_reader,
        )
        for key in (
            "runtime_version",
            "source_revision",
            "source_tree",
            "candidate_sha256",
            "initial_payload_closure",
            "requirements_lock_sha256",
            "expected_public_tool_count",
            "expected_public_surface_sha256",
            "substrate_manifest_schema",
            "candidate_handoff_schema",
            "payload_schema",
        ):
            if candidate[key] != final_candidate[key]:
                raise ReleaseBundleError("frozen candidate changed during release bundling")
        verify_release_bundle(
            archive_path,
            manifest_path,
            checksums_path,
            identity_reader=identity_reader,
        )

        _require_empty_directory(output_dir, "output directory")
        emitted: list[Path] = []
        try:
            for source in (archive_path, manifest_path, checksums_path):
                destination = output_dir / source.name
                os.replace(source, destination)
                emitted.append(destination)
        except OSError:
            for path in emitted:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    return {
        "archive": str(output_dir / filename),
        "manifest": str(output_dir / MANIFEST_NAME),
        "checksums": str(output_dir / CHECKSUMS_NAME),
        "candidate_sha256": candidate["candidate_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or verify deterministic Agent Runtime release assets."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--candidate-root", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--checksums", type=Path, required=True)
    verify.add_argument("--extract-dir", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.operation == "build":
            result = build_release_bundle(args.candidate_root, args.output_dir)
            print("RELEASE BUNDLE BUILD PASS")
            print(f"archive={result['archive']}")
            print(f"manifest={result['manifest']}")
            print(f"checksums={result['checksums']}")
            return 0
        result = verify_release_bundle(
            args.archive,
            args.manifest,
            args.checksums,
            extract_dir=args.extract_dir,
        )
        print("RELEASE BUNDLE VERIFY PASS")
        print(f"candidate_sha256={result['candidate_sha256']}")
        if result["extracted_root"] is not None:
            print(f"extracted_root={result['extracted_root']}")
        return 0
    except (ReleaseBundleError, provenance.PackageProvenanceError, OSError) as exc:
        print("RELEASE BUNDLE ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
