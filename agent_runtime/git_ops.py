from __future__ import annotations

import os
import subprocess
from typing import Any

from .config import ProjectProfile

GIT_FETCH_TIMEOUT_SECONDS = 75
GIT_LOCAL_TIMEOUT_SECONDS = 30
GIT_INSPECT_TIMEOUT_SECONDS = 15
MAX_COMMAND_OUTPUT_CHARS = 4000


class GitError(RuntimeError):
    """A bounded repository operation failed or a trusted identity check failed."""


def _automation_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    return env


def _bounded(value: str | bytes | None, limit: int = MAX_COMMAND_OUTPUT_CHARS) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-limit:]


def _git(
    profile: ProjectProfile,
    args: list[str],
    *,
    timeout: int = GIT_LOCAL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=profile.checkout,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env=_automation_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"Git operation timed out: git {' '.join(args)}; output={_bounded(exc.stdout)!r}"
        ) from exc
    except OSError as exc:
        raise GitError(f"Unable to run Git: {type(exc).__name__}: {exc}") from exc


def _git_value(profile: ProjectProfile, args: list[str], *, timeout: int) -> str:
    result = _git(profile, args, timeout=timeout)
    if result.returncode != 0:
        raise GitError(
            f"Git inspection failed: git {' '.join(args)}; exit={result.returncode}; "
            f"output={_bounded(result.stdout)!r}"
        )
    return result.stdout.strip()


def validate_remote_identity(profile: ProjectProfile) -> str:
    actual = _git_value(
        profile,
        ["remote", "get-url", profile.remote],
        timeout=GIT_INSPECT_TIMEOUT_SECONDS,
    )
    if actual != profile.expected_remote_url:
        raise GitError("Configured Git remote identity does not match trusted profile.")
    return actual


def inspect_repository(profile: ProjectProfile, *, require_remote_identity: bool = True) -> dict[str, Any]:
    try:
        head = _git_value(profile, ["rev-parse", "HEAD"], timeout=GIT_INSPECT_TIMEOUT_SECONDS)
        branch = _git_value(
            profile, ["branch", "--show-current"], timeout=GIT_INSPECT_TIMEOUT_SECONDS
        )
        status = _git_value(profile, ["status", "--porcelain"], timeout=GIT_INSPECT_TIMEOUT_SECONDS)
        remote_ref = f"{profile.remote}/{profile.branch}"
        cached_remote_head = _git_value(
            profile, ["rev-parse", remote_ref], timeout=GIT_INSPECT_TIMEOUT_SECONDS
        )
        remote_identity_ok = True
        if require_remote_identity:
            validate_remote_identity(profile)
    except GitError as exc:
        return {
            "ok": False,
            "repository": profile.repository,
            "configured_branch": profile.branch,
            "error": str(exc),
            "remote_identity_ok": False if "remote identity" in str(exc).lower() else None,
        }

    return {
        "ok": True,
        "repository": profile.repository,
        "configured_branch": profile.branch,
        "current_branch": branch,
        "head": head,
        "clean": status == "",
        "cached_remote_head": cached_remote_head,
        "in_sync": head == cached_remote_head,
        "remote_identity_ok": remote_identity_ok,
    }


def sync_checkout(profile: ProjectProfile) -> dict[str, Any]:
    if profile.disposable is not True:
        raise GitError("Destructive sync requires disposable = true in the trusted profile.")

    validate_remote_identity(profile)
    commands = [
        (["fetch", profile.remote, profile.branch, "--prune"], GIT_FETCH_TIMEOUT_SECONDS),
        (["switch", profile.branch], GIT_LOCAL_TIMEOUT_SECONDS),
        (["reset", "--hard", f"{profile.remote}/{profile.branch}"], GIT_LOCAL_TIMEOUT_SECONDS),
        (["clean", "-fd"], GIT_LOCAL_TIMEOUT_SECONDS),
    ]
    outputs: list[str] = []
    for args, timeout in commands:
        result = _git(profile, args, timeout=timeout)
        outputs.append(result.stdout.strip())
        if result.returncode != 0:
            raise GitError(
                f"Git sync failed: git {' '.join(args)}; exit={result.returncode}; "
                f"output={_bounded(result.stdout)!r}"
            )

    validate_remote_identity(profile)
    state = inspect_repository(profile)
    if not (
        state.get("ok")
        and state.get("current_branch") == profile.branch
        and state.get("clean") is True
        and state.get("in_sync") is True
        and state.get("remote_identity_ok") is True
    ):
        raise GitError("Post-sync repository invariants failed.")
    state["output"] = _bounded("\n".join(value for value in outputs if value))
    return state
