#!/usr/bin/env python3
"""Validate and initialize the per-user Agent Runtime configuration."""

import os
import re
import stat
import sys
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


def validate(path: Path, *, require_mode: bool) -> bytes:
    if path.is_symlink() or not path.is_file():
        fail(f"{path.name} must be a regular non-symlink file")
    if require_mode and stat.S_IMODE(path.stat().st_mode) != 0o600:
        fail(f"{path.name} must have mode 0600")
    try:
        raw = path.read_bytes()
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        fail(f"could not read {path.name}: {exc}")
    values = {}
    for number, line in enumerate(lines, start=1):
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
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        fail("missing non-empty value for " + ", ".join(missing))
    workspace = Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
    if not workspace.is_absolute() or not workspace.is_dir():
        fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory")
    return raw


def ensure(source: Path, canonical: Path) -> None:
    if canonical.exists() or canonical.is_symlink():
        validate(canonical, require_mode=True)
        return
    raw = validate(source, require_mode=False)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(canonical, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        validate(canonical, require_mode=True)
        return
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(canonical, 0o600)
    except BaseException:
        try:
            canonical.unlink()
        except OSError:
            pass
        raise


if __name__ == "__main__":
    if len(sys.argv) != 3:
        fail("usage: runtime_config.py SOURCE_ENV CANONICAL_ENV")
    ensure(Path(sys.argv[1]), Path(sys.argv[2]))
