from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_VERSION = 1
MAX_STATE_BYTES = 256 * 1024
MAX_LOG_TAIL_BYTES = 64 * 1024
ACTIVE_STATES = frozenset({"STARTING", "RUNNING"})
TERMINAL_STATES = frozenset({"PASS", "FAIL", "TIMEOUT", "INTERRUPTED", "LAUNCH_FAILED"})


class StateStore:
    def __init__(self, state_dir: Path, project_id: str):
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

    def append_wrapper_log(self, message: str) -> None:
        self.ensure_directory()
        with self.inprogress_log_path.open("ab", buffering=0) as handle:
            handle.write(message.encode("utf-8", errors="replace"))
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
