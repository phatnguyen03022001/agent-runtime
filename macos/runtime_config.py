#!/usr/bin/env python3
from __future__ import annotations

"""Validate and initialize the per-user Agent Runtime configuration."""

import json
import os
import re
import shutil
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
TUNNEL_ID = re.compile(r"tunnel_[0-9a-f]{32}")
MAX_TUNNEL_RESPONSE_BYTES = 16384
TUNNEL_LOOKUP_TIMEOUT_SECONDS = 10


class ConfigurationAdmissionError(Exception):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


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


def _normalized_prebuilt_workspace(workspace_root: Path) -> Path:
    if not workspace_root.is_absolute() or not workspace_root.is_dir():
        fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory")
    return workspace_root.resolve()


def prebuilt_values(
    workspace_root: Path,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    workspace = _normalized_prebuilt_workspace(workspace_root)
    env = os.environ if environ is None else environ
    values = {
        "CONTROL_PLANE_API_KEY": env.get("CONTROL_PLANE_API_KEY", ""),
        "CONTROL_PLANE_TUNNEL_ID": env.get("CONTROL_PLANE_TUNNEL_ID", ""),
        "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
        "AGENT_RUNTIME_GIT_NAME": env.get("AGENT_RUNTIME_GIT_NAME", ""),
        "AGENT_RUNTIME_GIT_EMAIL": env.get("AGENT_RUNTIME_GIT_EMAIL", ""),
    }
    for key in sorted(OPTIONAL):
        value = env.get(key)
        if value is not None and value != "":
            values[key] = value
    _validate_values(values, workspace)
    _validated_identity(values, required=True)
    return values


def _prebuilt_payload(values: dict[str, str]) -> bytes:
    ordered = [
        *REQUIRED,
        *GIT_IDENTITY,
        "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS",
        "AGENT_RUNTIME_MAX_PARALLELISM",
        "AGENT_RUNTIME_TELEMETRY",
    ]
    return "".join(f"{key}={values[key]}\n" for key in ordered if key in values).encode("utf-8")


def prebuilt_values_from_mapping(values: dict[str, object]) -> dict[str, str]:
    allowed = set(REQUIRED) | set(GIT_IDENTITY) | OPTIONAL
    if any(key not in allowed for key in values):
        fail("unsupported configuration field")
    if any(not isinstance(value, str) for value in values.values()):
        fail("configuration values must be strings")

    normalized = {key: str(value) for key, value in values.items()}
    workspace_value = normalized.get("AGENT_RUNTIME_WORKSPACE_ROOT", "")
    workspace = _normalized_prebuilt_workspace(Path(workspace_value))
    normalized["AGENT_RUNTIME_WORKSPACE_ROOT"] = str(workspace)
    _validate_values(normalized, workspace)
    _validated_identity(normalized, required=True)
    api_key = normalized["CONTROL_PLANE_API_KEY"]
    if any(character in api_key for character in ("\x00", "\r", "\n")):
        fail("CONTROL_PLANE_API_KEY is malformed")
    if TUNNEL_ID.fullmatch(normalized["CONTROL_PLANE_TUNNEL_ID"]) is None:
        fail("CONTROL_PLANE_TUNNEL_ID must match tunnel_<32 lowercase hex characters>")
    return normalized


def _admission_values(values: dict[str, object]) -> dict[str, str]:
    try:
        return prebuilt_values_from_mapping(values)
    except SystemExit as exc:
        message = str(exc)
        if "AGENT_RUNTIME_WORKSPACE_ROOT" in message or "absolute existing directory" in message:
            reason = "WORKSPACE_UNAVAILABLE"
        elif "CONTROL_PLANE_TUNNEL_ID" in message:
            reason = "INVALID_TUNNEL_ID"
        else:
            reason = "RUNTIME_CONFIGURATION_INCOMPLETE"
        raise ConfigurationAdmissionError(reason, message.removeprefix("CONFIG ERROR: ")) from None


def _safe_tunnel_environment(source: dict[str, str], api_key: str) -> dict[str, str]:
    child: dict[str, str] = {}
    for key in ("HOME", "USER", "TMPDIR", "LANG", "PATH"):
        value = source.get(key)
        if value:
            child[key] = value
    for key, value in source.items():
        if key.startswith("LC_") and value:
            child[key] = value
    child["CONTROL_PLANE_API_KEY"] = api_key
    return child


def _http_status_from_json(value: object) -> int | None:
    pending = [value]
    visited = 0
    while pending and visited < 64:
        current = pending.pop()
        visited += 1
        if isinstance(current, dict):
            for key, item in current.items():
                if key in {"status", "status_code", "statusCode", "http_status", "httpStatus"}:
                    if isinstance(item, int) and 100 <= item <= 599:
                        return item
                    if isinstance(item, str) and item.isdigit() and 100 <= int(item) <= 599:
                        return int(item)
                if isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(current, list):
            pending.extend(item for item in current if isinstance(item, (dict, list)))
    return None


def validate_tunnel_access(
    values: dict[str, str],
    *,
    environ: dict[str, str] | None = None,
) -> None:
    source = dict(os.environ if environ is None else environ)
    client = shutil.which("tunnel-client", path=source.get("PATH"))
    if client is None:
        raise ConfigurationAdmissionError(
            "TUNNEL_CLIENT_UNAVAILABLE",
            "Install the official OpenAI tunnel-client and try again.",
        )
    client_path = Path(client)
    if not client_path.is_absolute() or not client_path.is_file() or not os.access(client_path, os.X_OK):
        raise ConfigurationAdmissionError(
            "TUNNEL_CLIENT_UNAVAILABLE",
            "Install the official OpenAI tunnel-client and try again.",
        )

    api_key = values.get("CONTROL_PLANE_API_KEY", "")
    if not api_key or any(character in api_key for character in ("\x00", "\r", "\n")):
        raise ConfigurationAdmissionError(
            "RUNTIME_CONFIGURATION_INCOMPLETE",
            "Enter a valid Runtime API key.",
        )
    tunnel_id = values["CONTROL_PLANE_TUNNEL_ID"]
    if TUNNEL_ID.fullmatch(tunnel_id) is None:
        raise ConfigurationAdmissionError("INVALID_TUNNEL_ID", "Enter a valid Runtime Tunnel ID.")

    try:
        result = subprocess.run(
            [str(client_path), "admin", "--json", "tunnels", "get", tunnel_id],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=TUNNEL_LOOKUP_TIMEOUT_SECONDS,
            env=_safe_tunnel_environment(source, api_key),
        )
    except (OSError, subprocess.SubprocessError):
        raise ConfigurationAdmissionError(
            "CONTROL_PLANE_UNAVAILABLE",
            "Tunnel validation is temporarily unavailable. Try again later.",
        ) from None

    if any(
        len(stream.encode("utf-8", errors="ignore")) > MAX_TUNNEL_RESPONSE_BYTES
        for stream in (result.stdout, result.stderr)
    ):
        raise ConfigurationAdmissionError(
            "CONTROL_PLANE_UNAVAILABLE",
            "Tunnel validation returned an invalid response. Try again later.",
        )
    response: object | None = None
    for stream in (result.stdout, result.stderr):
        try:
            candidate = json.loads(stream)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, (dict, list)):
            response = candidate
            break

    if result.returncode == 0:
        if not isinstance(response, dict):
            raise ConfigurationAdmissionError(
                "CONTROL_PLANE_UNAVAILABLE",
                "Tunnel validation returned an invalid response. Try again later.",
            )
        return

    status = _http_status_from_json(response)
    if status == 401:
        reason = "INVALID_CREDENTIAL"
        message = "The Runtime API key was rejected. Create or check the key and try again."
    elif status == 403:
        reason = "TUNNEL_ACCESS_DENIED"
        message = "The Runtime API key does not have access to that tunnel."
    elif status == 404:
        reason = "TUNNEL_NOT_FOUND"
        message = "The configured tunnel was not found."
    else:
        reason = "CONTROL_PLANE_UNAVAILABLE"
        message = "Tunnel validation is temporarily unavailable. Try again later."
    raise ConfigurationAdmissionError(reason, message)


def _publish_admitted_prebuilt(
    canonical: Path,
    normalized: dict[str, str],
    *,
    replace_existing: bool,
) -> None:
    workspace = Path(normalized["AGENT_RUNTIME_WORKSPACE_ROOT"])
    previous: bytes | None = None
    if replace_existing:
        if not canonical.exists() or canonical.is_symlink():
            raise ConfigurationAdmissionError(
                "RUNTIME_CONFIGURATION_INCOMPLETE",
                "Existing canonical Runtime configuration is unavailable or unsafe.",
            )
        try:
            previous = validate(canonical, require_mode=True, require_git_identity=True)
        except SystemExit:
            raise ConfigurationAdmissionError(
                "RUNTIME_CONFIGURATION_INCOMPLETE",
                "Existing canonical Runtime configuration is unavailable or unsafe.",
            ) from None
    elif canonical.exists() or canonical.is_symlink():
        raise ConfigurationAdmissionError(
            "RUNTIME_CONFIGURATION_INCOMPLETE",
            "Canonical Runtime configuration already exists; use explicit Reconfigure.",
        )

    raw = _prebuilt_payload(normalized)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    private = _write_private(canonical, raw)
    try:
        validate_prebuilt_configuration(private, workspace)
        if replace_existing:
            try:
                current = validate(canonical, require_mode=True, require_git_identity=True)
            except SystemExit:
                raise ConfigurationAdmissionError(
                    "RUNTIME_CONFIGURATION_INCOMPLETE",
                    "Existing canonical Runtime configuration changed before publication.",
                ) from None
            if current != previous:
                raise ConfigurationAdmissionError(
                    "RUNTIME_CONFIGURATION_INCOMPLETE",
                    "Existing canonical Runtime configuration changed before publication.",
                )
            os.replace(private, canonical)
        else:
            try:
                os.link(private, canonical)
            except FileExistsError:
                raise ConfigurationAdmissionError(
                    "RUNTIME_CONFIGURATION_INCOMPLETE",
                    "Canonical Runtime configuration appeared before publication; use explicit Reconfigure.",
                ) from None
    finally:
        try:
            private.unlink()
        except FileNotFoundError:
            pass


def admit_prebuilt_values(
    canonical: Path,
    values: dict[str, object],
    *,
    replace_existing: bool,
    environ: dict[str, str] | None = None,
) -> None:
    normalized = _admission_values(values)
    validate_tunnel_access(normalized, environ=environ)
    _publish_admitted_prebuilt(canonical, normalized, replace_existing=replace_existing)


def inspect_prebuilt_existing(canonical: Path) -> dict[str, object]:
    _, _, values = _read(canonical, require_mode=True)
    _validate_values(values)
    _validated_identity(values, required=True)
    workspace = _normalized_prebuilt_workspace(Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"]))
    return {
        "git_identity_ready": True,
        "workspace_root": str(workspace),
    }


def validate_prebuilt_configuration(canonical: Path, workspace_root: Path) -> bytes:
    workspace = _normalized_prebuilt_workspace(workspace_root)
    raw, _, values = _read(canonical, require_mode=True)
    _validate_values(values)
    _validated_identity(values, required=True)
    configured_workspace = Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
    if not configured_workspace.is_absolute() or configured_workspace.resolve() != workspace:
        fail("canonical Runtime workspace does not match explicit prebuilt workspace")
    return raw


def ensure_prebuilt_values(
    canonical: Path,
    values: dict[str, object],
    *,
    environ: dict[str, str] | None = None,
) -> None:
    admit_prebuilt_values(
        canonical,
        values,
        replace_existing=False,
        environ=environ,
    )


def ensure_prebuilt(
    canonical: Path,
    workspace_root: Path,
    *,
    environ: dict[str, str] | None = None,
) -> None:
    workspace = _normalized_prebuilt_workspace(workspace_root)
    if canonical.exists() or canonical.is_symlink():
        validate_prebuilt_configuration(canonical, workspace)
        return

    values = prebuilt_values(workspace, environ=environ)
    admit_prebuilt_values(canonical, values, replace_existing=False, environ=environ)


def _native_admission(canonical: Path, *, replace_existing: bool) -> int:
    payload = sys.stdin.buffer.read(65537)
    try:
        if len(payload) > 65536:
            raise ConfigurationAdmissionError(
                "RUNTIME_CONFIGURATION_INCOMPLETE",
                "Native configuration input is too large.",
            )
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ConfigurationAdmissionError(
                "RUNTIME_CONFIGURATION_INCOMPLETE",
                "Native configuration input is malformed.",
            ) from None
        if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
            raise ConfigurationAdmissionError(
                "RUNTIME_CONFIGURATION_INCOMPLETE",
                "Native configuration input must be an object.",
            )
        admit_prebuilt_values(
            canonical,
            decoded,
            replace_existing=replace_existing,
        )
    except ConfigurationAdmissionError as exc:
        sys.stdout.write(json.dumps({
            "schema_version": 1,
            "status": "error",
            "reason_code": exc.reason_code,
            "message": exc.message,
        }, separators=(",", ":"), sort_keys=True) + "\n")
        return 2
    sys.stdout.write(json.dumps({
        "schema_version": 1,
        "status": "ok",
        "reason_code": "OK",
    }, separators=(",", ":"), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in {"--prebuilt-stdin", "--reconfigure-stdin"}:
        raise SystemExit(_native_admission(
            Path(sys.argv[2]),
            replace_existing=sys.argv[1] == "--reconfigure-stdin",
        ))
    elif len(sys.argv) == 3 and sys.argv[1] == "--inspect-prebuilt-existing":
        inspected = inspect_prebuilt_existing(Path(sys.argv[2]))
        sys.stdout.write(json.dumps(inspected, separators=(",", ":"), sort_keys=True) + "\n")
    elif len(sys.argv) == 4 and sys.argv[1] == "--prebuilt":
        ensure_prebuilt(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 4:
        ensure(
            Path(sys.argv[1]),
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            require_git_identity=True,
        )
    else:
        fail(
            "usage: runtime_config.py SOURCE_ENV CANONICAL_ENV WORKSPACE_ROOT, "
            "runtime_config.py --prebuilt CANONICAL_ENV WORKSPACE_ROOT, "
            "runtime_config.py --prebuilt-stdin CANONICAL_ENV, "
            "runtime_config.py --reconfigure-stdin CANONICAL_ENV, or "
            "runtime_config.py --inspect-prebuilt-existing CANONICAL_ENV"
        )
