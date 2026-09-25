#!/usr/bin/env python3
from __future__ import annotations

"""Validate and initialize the per-user Agent Runtime configuration."""

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REQUIRED = (
    "CONTROL_PLANE_API_KEY",
    "CONTROL_PLANE_TUNNEL_ID",
    "AGENT_RUNTIME_WORKSPACE_ROOT",
)
GIT_IDENTITY = ("AGENT_RUNTIME_GIT_NAME", "AGENT_RUNTIME_GIT_EMAIL")
OPTIONAL = {"AGENT_RUNTIME_MAX_ACTIVE_SESSIONS", "AGENT_RUNTIME_MAX_PARALLELISM", "AGENT_RUNTIME_TELEMETRY"}
ENTRY = re.compile(r"([A-Z_][A-Z0-9_]*)=(.*)")
MAX_ACTIVE_SESSIONS = 6
MAX_IDENTITY_BYTES = 256


def fail(message: str) -> "NoReturn":
    raise SystemExit("CONFIG ERROR: " + message)


def _read(path: Path, *, require_mode: bool) -> tuple[bytes, list[str], dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        fail(f"{path.name} must be a regular non-symlink file")
    if require_mode and stat.S_IMODE(path.stat().st_mode) != 0o600:
        fail(f"{path.name} must have mode 0600")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"could not read {path.name}: {exc}")
    lines = text.splitlines(keepends=True)
    values: dict[str, str] = {}
    tracked = set(REQUIRED) | OPTIONAL | set(GIT_IDENTITY)
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if not line or line.lstrip().startswith("#"):
            continue
        match = ENTRY.fullmatch(line)
        if match is None:
            fail(f"malformed configuration entry at line {number}")
        key, value = match.groups()
        if key in tracked:
            if key in values:
                fail(f"duplicate {key} entry")
            values[key] = value
    return raw, lines, values


def _identity_value_valid(value: str | None) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return len(raw) <= MAX_IDENTITY_BYTES and not any(ch in value for ch in ("\x00", "\r", "\n"))


def _validated_identity(values: dict[str, str], *, required: bool) -> tuple[str, str] | None:
    present = tuple(key in values for key in GIT_IDENTITY)
    if not any(present):
        if required:
            fail("Runtime Git identity is unavailable")
        return None
    if not all(present):
        fail("Runtime Git identity must provide both name and email")
    pair = (values[GIT_IDENTITY[0]], values[GIT_IDENTITY[1]])
    if not all(_identity_value_valid(value) for value in pair):
        fail("Runtime Git identity is missing or malformed")
    return pair


def _validate_values(values: dict[str, str], workspace_root: Path | None = None) -> None:
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        fail("missing non-empty value for " + ", ".join(missing))
    workspace = workspace_root if workspace_root is not None else Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
    if not workspace.is_absolute() or not workspace.is_dir():
        fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory")
    parallelism = values.get("AGENT_RUNTIME_MAX_PARALLELISM")
    if parallelism is not None and (
        re.fullmatch(r"[1-9][0-9]*", parallelism) is None or not 1 <= int(parallelism) <= 10
    ):
        fail("AGENT_RUNTIME_MAX_PARALLELISM must be an integer from 1 through 10")
    session_limit = values.get("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS")
    if session_limit is not None and (
        re.fullmatch(r"[1-9][0-9]*", session_limit) is None
        or not 1 <= int(session_limit) <= MAX_ACTIVE_SESSIONS
    ):
        fail(f"AGENT_RUNTIME_MAX_ACTIVE_SESSIONS must be an integer from 1 through {MAX_ACTIVE_SESSIONS}")
    telemetry_mode = values.get("AGENT_RUNTIME_TELEMETRY", "off")
    if telemetry_mode not in {"off", "otlp"}:
        fail("AGENT_RUNTIME_TELEMETRY must be off or otlp")


def validate(path: Path, *, require_mode: bool, require_git_identity: bool = False) -> bytes:
    raw, _, values = _read(path, require_mode=require_mode)
    _validate_values(values)
    _validated_identity(values, required=require_git_identity)
    return raw


def _git_config_value(source: Path, key: str) -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(source.parent), "config", "--local", "--get", key],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\r\n")


def _resolve_git_identity(source: Path) -> tuple[str, str]:
    explicit = tuple(os.environ.get(key, "") for key in GIT_IDENTITY)
    if any(explicit):
        if not all(explicit) or not all(_identity_value_valid(value) for value in explicit):
            fail("installer Runtime Git identity must provide one valid name/email pair")
        return explicit[0], explicit[1]

    local = (
        _git_config_value(source, "user.name"),
        _git_config_value(source, "user.email"),
    )
    if not all(local) or not all(_identity_value_valid(value) for value in local):
        fail("repository-local Runtime Git identity is unavailable or malformed")
    return local[0], local[1]


def _bootstrap_payload(
    source: Path,
    workspace_root: Path,
    *,
    git_identity: tuple[str, str] | None = None,
) -> bytes:
    _, lines, values = _read(source, require_mode=False)
    inherited = {key: os.environ.get(key, "") for key in REQUIRED[:2]}
    resolved = {key: values.get(key, "") or inherited[key] for key in REQUIRED[:2]}
    missing = [key for key in REQUIRED[:2] if not resolved[key]]
    if missing:
        fail("missing non-empty value for " + ", ".join(missing))
    if "AGENT_RUNTIME_WORKSPACE_ROOT" not in values:
        fail("missing AGENT_RUNTIME_WORKSPACE_ROOT entry")
    _validate_values({**values, **resolved, "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace_root)}, workspace_root)
    _validated_identity(values, required=False)

    replacements = {**resolved, "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace_root)}
    if git_identity is not None:
        replacements.update(dict(zip(GIT_IDENTITY, git_identity)))

    rendered: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        match = ENTRY.fullmatch(line)
        if match is None or match.group(1) not in replacements:
            rendered.append(raw_line)
            continue
        key = match.group(1)
        seen.add(key)
        ending = raw_line[len(line):]
        rendered.append(f"{key}={replacements[key]}{ending}")
    for key in (*REQUIRED[:2], *GIT_IDENTITY):
        if key not in replacements or key in seen:
            continue
        if rendered and not rendered[-1].endswith(("\n", "\r")):
            rendered[-1] += "\n"
        rendered.append(f"{key}={replacements[key]}\n")
    return "".join(rendered).encode("utf-8")


def _append_git_identity(raw: bytes, identity: tuple[str, str]) -> bytes:
    suffix = b"" if not raw or raw.endswith((b"\n", b"\r")) else b"\n"
    suffix += (
        f"{GIT_IDENTITY[0]}={identity[0]}\n"
        f"{GIT_IDENTITY[1]}={identity[1]}\n"
    ).encode("utf-8")
    return raw + suffix


def _write_private(canonical: Path, raw: bytes) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f".{canonical.name}.", suffix=".tmp", dir=canonical.parent)
    private = Path(temp_name)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return private


def _migrate_existing_identity(source: Path, canonical: Path, raw: bytes) -> None:
    identity = _resolve_git_identity(source)
    migrated = _append_git_identity(raw, identity)
    private = _write_private(canonical, migrated)
    try:
        validate(private, require_mode=True, require_git_identity=True)
        current = validate(canonical, require_mode=True)
        if current != raw:
            fail("canonical Runtime configuration changed during Git identity migration")
        os.replace(private, canonical)
    finally:
        try:
            private.unlink()
        except FileNotFoundError:
            pass


def ensure(
    source: Path,
    canonical: Path,
    workspace_root: Path,
    *,
    require_git_identity: bool = False,
) -> None:
    if canonical.exists() or canonical.is_symlink():
        raw, _, values = _read(canonical, require_mode=True)
        _validate_values(values)
        identity = _validated_identity(values, required=False)
        if not require_git_identity or identity is not None:
            return
        _migrate_existing_identity(source, canonical, raw)
        return

    identity = _resolve_git_identity(source) if require_git_identity else None
    raw = _bootstrap_payload(source, workspace_root, git_identity=identity)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    private = _write_private(canonical, raw)
    try:
        if require_git_identity:
            validate(private, require_mode=True, require_git_identity=True)
        else:
            validate(private, require_mode=True)
        try:
            os.link(private, canonical)
        except FileExistsError:
            if require_git_identity:
                ensure(source, canonical, workspace_root, require_git_identity=True)
            else:
                validate(canonical, require_mode=True)
            return
    finally:
        try:
            private.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    if len(sys.argv) != 4:
        fail("usage: runtime_config.py SOURCE_ENV CANONICAL_ENV WORKSPACE_ROOT")
    ensure(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        Path(sys.argv[3]),
        require_git_identity=True,
    )
