#!/usr/bin/env python3
"""Validate and initialize the per-user Agent Runtime configuration."""

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

REQUIRED = (
    "CONTROL_PLANE_API_KEY",
    "CONTROL_PLANE_TUNNEL_ID",
    "AGENT_RUNTIME_WORKSPACE_ROOT",
)
OPTIONAL = {"AGENT_RUNTIME_MAX_ACTIVE_SESSIONS"}
ENTRY = re.compile(r"([A-Z_][A-Z0-9_]*)=(.*)")


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
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.rstrip("\r\n")
        if not line or line.lstrip().startswith("#"):
            continue
        match = ENTRY.fullmatch(line)
        if match is None:
            fail(f"malformed configuration entry at line {number}")
        key, value = match.groups()
        if key in REQUIRED or key in OPTIONAL:
            if key in values:
                fail(f"duplicate {key} entry")
            values[key] = value
    return raw, lines, values


def validate(path: Path, *, require_mode: bool) -> bytes:
    raw, _, values = _read(path, require_mode=require_mode)
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        fail("missing non-empty value for " + ", ".join(missing))
    workspace = Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
    if not workspace.is_absolute() or not workspace.is_dir():
        fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory")
    return raw


def _bootstrap_payload(source: Path, workspace_root: Path) -> bytes:
    _, lines, values = _read(source, require_mode=False)
    missing = [key for key in REQUIRED[:2] if not values.get(key)]
    if missing:
        fail("missing non-empty value for " + ", ".join(missing))
    if "AGENT_RUNTIME_WORKSPACE_ROOT" not in values:
        fail("missing AGENT_RUNTIME_WORKSPACE_ROOT entry")
    if not workspace_root.is_absolute() or not workspace_root.is_dir():
        fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory")

    rendered: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        match = ENTRY.fullmatch(line)
        if match is None or match.group(1) != "AGENT_RUNTIME_WORKSPACE_ROOT":
            rendered.append(raw_line)
            continue
        ending = raw_line[len(line):]
        rendered.append(f"AGENT_RUNTIME_WORKSPACE_ROOT={workspace_root}{ending}")
    return "".join(rendered).encode("utf-8")


def ensure(source: Path, canonical: Path, workspace_root: Path) -> None:
    if canonical.exists() or canonical.is_symlink():
        validate(canonical, require_mode=True)
        return
    raw = _bootstrap_payload(source, workspace_root)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{canonical.name}.", suffix=".tmp", dir=canonical.parent)
    private = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        validate(private, require_mode=True)
        try:
            os.link(private, canonical)
        except FileExistsError:
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
    ensure(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
