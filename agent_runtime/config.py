from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600
MAX_PROJECT_ID_LENGTH = 64
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOP_LEVEL_KEYS = frozenset({"version", "state_dir", "projects"})
_PROFILE_KEYS = frozenset(
    {
        "repository",
        "checkout",
        "remote",
        "expected_remote_url",
        "branch",
        "verify_argv",
        "timeout_seconds",
        "disposable",
    }
)


class ConfigError(ValueError):
    """Trusted local configuration is missing or invalid."""


@dataclass(frozen=True)
class ProjectProfile:
    project_id: str
    repository: str
    checkout: Path
    remote: str
    expected_remote_url: str
    branch: str
    verify_argv: tuple[str, ...]
    timeout_seconds: int
    disposable: bool


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path
    state_dir: Path
    projects: dict[str, ProjectProfile]

    def resolve(self, project_id: str) -> ProjectProfile:
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ConfigError("Unknown project ID.")
        try:
            return self.projects[project_id]
        except KeyError as exc:
            raise ConfigError("Unknown project ID.") from exc


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string.")
    if "\x00" in value:
        raise ConfigError(f"{field} contains a NUL byte.")
    return value


def _require_absolute_path(value: Any, field: str) -> Path:
    raw = _require_nonempty_string(value, field)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{field} must be absolute.")
    return path.resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _parse_profile(project_id: str, raw: Any) -> ProjectProfile:
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ConfigError(
            f"Project ID must match {_PROJECT_ID_RE.pattern!r} and be at most "
            f"{MAX_PROJECT_ID_LENGTH} characters."
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"projects.{project_id} must be a TOML table.")

    unknown = set(raw) - _PROFILE_KEYS
    missing = _PROFILE_KEYS - set(raw)
    if unknown:
        raise ConfigError(f"projects.{project_id} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ConfigError(f"projects.{project_id} is missing fields: {sorted(missing)}")

    verify_argv_raw = raw["verify_argv"]
    if (
        not isinstance(verify_argv_raw, list)
        or not verify_argv_raw
        or any(not isinstance(item, str) or not item for item in verify_argv_raw)
    ):
        raise ConfigError(f"projects.{project_id}.verify_argv must be a non-empty list[str].")
    if any("\x00" in item for item in verify_argv_raw):
        raise ConfigError(f"projects.{project_id}.verify_argv contains a NUL byte.")

    timeout = raw["timeout_seconds"]
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout < MIN_TIMEOUT_SECONDS
        or timeout > MAX_TIMEOUT_SECONDS
    ):
        raise ConfigError(
            f"projects.{project_id}.timeout_seconds must be an integer from "
            f"{MIN_TIMEOUT_SECONDS} to {MAX_TIMEOUT_SECONDS}."
        )

    disposable = raw["disposable"]
    if type(disposable) is not bool:
        raise ConfigError(f"projects.{project_id}.disposable must be a boolean.")

    return ProjectProfile(
        project_id=project_id,
        repository=_require_nonempty_string(raw["repository"], f"projects.{project_id}.repository"),
        checkout=_require_absolute_path(raw["checkout"], f"projects.{project_id}.checkout"),
        remote=_require_nonempty_string(raw["remote"], f"projects.{project_id}.remote"),
        expected_remote_url=_require_nonempty_string(
            raw["expected_remote_url"], f"projects.{project_id}.expected_remote_url"
        ),
        branch=_require_nonempty_string(raw["branch"], f"projects.{project_id}.branch"),
        verify_argv=tuple(verify_argv_raw),
        timeout_seconds=timeout,
        disposable=disposable,
    )


def load_config(path: str | Path) -> RuntimeConfig:
    config_path = Path(path).expanduser().resolve(strict=False)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError("Unable to load runtime config.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("Malformed runtime config.") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Runtime config root must be a TOML table.")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(raw)
    if unknown:
        raise ConfigError(f"Runtime config has unknown fields: {sorted(unknown)}")
    if missing:
        raise ConfigError(f"Runtime config is missing fields: {sorted(missing)}")
    if raw["version"] != CONFIG_VERSION or type(raw["version"]) is not int:
        raise ConfigError(f"Unsupported config version: {raw['version']!r}")

    state_dir = _require_absolute_path(raw["state_dir"], "state_dir")
    projects_raw = raw["projects"]
    if not isinstance(projects_raw, dict) or not projects_raw:
        raise ConfigError("projects must be a non-empty TOML table.")

    projects = {
        project_id: _parse_profile(project_id, profile_raw)
        for project_id, profile_raw in projects_raw.items()
    }

    folded_ids: set[str] = set()
    for project_id in projects:
        folded = project_id.casefold()
        if folded in folded_ids:
            raise ConfigError("Project IDs must be unique when case-folded.")
        folded_ids.add(folded)

    profiles = list(projects.values())
    for profile in profiles:
        if _paths_overlap(state_dir, profile.checkout):
            raise ConfigError("state_dir must not overlap a project checkout.")
    for index, first in enumerate(profiles):
        for second in profiles[index + 1 :]:
            if _paths_overlap(first.checkout, second.checkout):
                raise ConfigError("Project checkouts must not overlap.")

    return RuntimeConfig(path=config_path, state_dir=state_dir, projects=projects)
