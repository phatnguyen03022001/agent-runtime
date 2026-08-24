from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_VERSION = 1
MAX_STATE_BYTES = 256 * 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
MAX_VERIFY_LOG_BYTES = 1024 * 1024
ACTIVE_STATES = frozenset({"STARTING", "RUNNING"})
TERMINAL_STATES = frozenset({"PASS", "FAIL", "TIMEOUT", "INTERRUPTED", "LAUNCH_FAILED"})
_ALL_STATES = ACTIVE_STATES | TERMINAL_STATES
_PASS_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PASS_POSTCONDITIONS = ("same_head", "configured_branch", "clean", "in_sync", "remote_identity")


def _validate_persisted_state(value: dict[str, Any], project_id: str, finalized_log_path: Path) -> str | None:
    def positive_int(item: Any) -> bool:
        return type(item) is int and item > 0

    if type(value.get("version")) is not int or value.get("version") != STATE_VERSION:
        return "Verification state failed semantic validation."
    if value.get("project") != project_id:
        return "Verification state failed semantic validation."
    status_value = value.get("status")
    if status_value not in _ALL_STATES:
        return "Verification state failed semantic validation."
    if value.get("verification_ok") is True and status_value != "PASS":
        return "Verification state failed semantic validation."
    if status_value != "PASS":
        return None
    if value.get("verification_ok") is not True:
        return "Verification state failed semantic validation."

    run_id = value.get("run_id")
    if not isinstance(run_id, str) or _PASS_RUN_ID_RE.fullmatch(run_id) is None:
        return "Verification state failed semantic validation."
    if type(value.get("exit_code")) is not int or value.get("exit_code") != 0:
        return "Verification state failed semantic validation."
    if value.get("timed_out") is not False or value.get("failure_kind") is not None:
        return "Verification state failed semantic validation."
    if not isinstance(value.get("head"), str) or not value["head"]:
        return "Verification state failed semantic validation."

    started_at = value.get("started_at")
    finished_at = value.get("finished_at")
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return "Verification state failed semantic validation."
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
        if finished < started:
            return "Verification state failed semantic validation."
    except (TypeError, ValueError):
        return "Verification state failed semantic validation."

    duration = value.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration < 0
    ):
        return "Verification state failed semantic validation."
    if not positive_int(value.get("timeout_seconds")):
        return "Verification state failed semantic validation."
    for field in ("launcher_pid", "worker_pid", "verify_pid", "verify_pgid"):
        if not positive_int(value.get(field)):
            return "Verification state failed semantic validation."
    if value.get("verify_pid") != value.get("verify_pgid"):
        return "Verification state failed semantic validation."

    working_tree = value.get("working_tree_after")
    if not isinstance(working_tree, dict) or working_tree.get("ok") is not True:
        return "Verification state failed semantic validation."
    state_head = value["head"]
    if working_tree.get("head") != state_head or working_tree.get("cached_remote_head") != state_head:
        return "Verification state failed semantic validation."
    current_branch = working_tree.get("current_branch")
    configured_branch = working_tree.get("configured_branch")
    if (
        not isinstance(current_branch, str)
        or not current_branch
        or not isinstance(configured_branch, str)
        or not configured_branch
        or current_branch != configured_branch
    ):
        return "Verification state failed semantic validation."
    if (
        working_tree.get("clean") is not True
        or working_tree.get("in_sync") is not True
        or working_tree.get("remote_identity_ok") is not True
    ):
        return "Verification state failed semantic validation."
    postconditions = value.get("postconditions")
    if not isinstance(postconditions, dict):
        return "Verification state failed semantic validation."
    if any(postconditions.get(name) is not True for name in _PASS_POSTCONDITIONS):
        return "Verification state failed semantic validation."
    if value.get("log_run_id") != run_id or value.get("log_finalized") is not True:
        return "Verification state failed semantic validation."

    try:
        log_stat = finalized_log_path.stat()
    except OSError:
        return "Verification state failed semantic validation."
    if not stat.S_ISREG(log_stat.st_mode) or log_stat.st_size > MAX_VERIFY_LOG_BYTES:
        return "Verification state failed semantic validation."
    return None


class StateStore:
    def __init__(self, state_dir: Path, project_id: str):
        self.project_id = project_id
        self.root = (state_dir / project_id).resolve(strict=False)
        self.lock_path = self.root / "runner.lock"
        self.state_path = self.root / "verify-state.json"
        self.state_tmp_path = self.root / "verify-state.json.tmp"
        self.log_path = self.root / "last-verify.log"
        self.inprogress_log_path = self.root / "last-verify.log.inprogress"

    def ensure_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _fsync_directory(self) -> None:
        try:
            fd = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def read_state(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            with self.state_path.open("rb") as handle:
                raw = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"Unable to read verification state: {type(exc).__name__}: {exc}"
        if len(raw) > MAX_STATE_BYTES:
            return None, f"Verification state exceeds {MAX_STATE_BYTES} bytes."
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            return None, f"Unable to read verification state: {type(exc).__name__}: {exc}"
        if not isinstance(value, dict):
            return None, "Unable to read verification state: JSON root is not an object."
        semantic_error = _validate_persisted_state(value, self.project_id, self.log_path)
        if semantic_error is not None:
            return None, semantic_error
        return value, None

    def write_state(self, state: dict[str, Any]) -> None:
        self.ensure_directory()
        payload = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        encoded = payload.encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise ValueError(f"Verification state exceeds {MAX_STATE_BYTES} bytes.")
        with self.state_tmp_path.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(self.state_tmp_path, self.state_path)
        self._fsync_directory()

    def prepare_log(self) -> None:
        self.ensure_directory()
        with self.inprogress_log_path.open("wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        self._fsync_directory()

    def append_log_bytes(
        self, handle: Any, data: bytes, *, reserve_bytes: int = 0
    ) -> int:
        if reserve_bytes < 0 or reserve_bytes > MAX_VERIFY_LOG_BYTES:
            raise ValueError("Invalid verifier log reserve.")
        handle.seek(0, os.SEEK_END)
        current = handle.tell()
        limit = MAX_VERIFY_LOG_BYTES - reserve_bytes
        if current >= limit:
            return 0
        payload = data[: limit - current]
        written = handle.write(payload)
        return len(payload) if written is None else int(written)

    def append_wrapper_log(self, message: str) -> None:
        self.ensure_directory()
        with self.inprogress_log_path.open("ab", buffering=0) as handle:
            self.append_log_bytes(handle, message.encode("utf-8", errors="replace"))
            os.fsync(handle.fileno())

    def finalize_log(self) -> None:
        fd = os.open(self.inprogress_log_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(self.inprogress_log_path, self.log_path)
        self._fsync_directory()

    def commit_terminal_with_log(self, state: dict[str, Any]) -> None:
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id or state.get("log_run_id") != run_id:
            raise ValueError("Terminal state/log run association is invalid.")
        terminal = dict(state)
        terminal["log_finalized"] = True
        self.finalize_log()
        self.write_state(terminal)

    def read_log_tail(self, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            read_size = min(size, MAX_LOG_TAIL_BYTES)
            handle.seek(size - read_size)
            raw = handle.read(read_size)
        text = raw.decode("utf-8", errors="replace")
        encoded = text.encode("utf-8")
        decode_truncated = False
        if len(encoded) > MAX_LOG_TAIL_BYTES:
            text = encoded[-MAX_LOG_TAIL_BYTES:].decode("utf-8", errors="ignore")
            decode_truncated = True
        return {
            "log_tail": text,
            "tail_truncated": size > MAX_LOG_TAIL_BYTES or decode_truncated,
            "log_bytes_returned": len(text.encode("utf-8")),
        }
