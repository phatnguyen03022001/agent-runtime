from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tool_contract import canonical_structured_bytes


DURABLE_STATE_SCHEMA_VERSION = 1
DURABLE_JOURNAL_SCHEMA_VERSION = 1
DURABLE_SPEC_SCHEMA_VERSION = 1
DURABLE_CONTROL_SCHEMA_VERSION = 1
DURABLE_INTEGRITY_VERSION = 1

DURABLE_ROOT_ENV = "AGENT_RUNTIME_DURABLE_STATE_ROOT"
DURABLE_ROOT_MODE = 0o700
DURABLE_JOB_MODE = 0o700
DURABLE_FILE_MODE = 0o600
DURABLE_SCAN_LIMIT = 24
MAX_STATE_BYTES = 64 * 1024
MAX_SPEC_BYTES = 512 * 1024
MAX_JOURNAL_BYTES = 256 * 1024
MAX_CONTROL_BYTES = 1024
MAX_RETAINED_OUTPUT_BYTES = 64 * 1024
_SNAPSHOT_READ_ATTEMPTS = 8
_SNAPSHOT_RETRY_SECONDS = 0.01

_JOB_ID = re.compile(r"^[0-9a-f]{64}$")
_START_IDENTITY = re.compile(r"^[0-9a-f]{32}$")
_SESSION_ID = re.compile(r"^[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_STREAMS = frozenset({"stdout", "stderr"})
_ALLOWED_LIFECYCLES = frozenset(
    {
        "STARTING",
        "RUNNING",
        "COMPLETED",
        "START_FAILED_PRE_EFFECT",
        "START_FAILED_POST_EFFECT",
    }
)
_ALLOWED_STATUS = frozenset({"starting", "running", "exited"})
_ALLOWED_TERMINATION = frozenset(
    {
        "natural_exit",
        "explicit_terminate",
        "hard_wall_timeout",
        "start_failed_pre_effect",
        "start_failed_post_effect",
    }
)
_PROC_PIDTBSDINFO = 3
_PROC_PIDPATHINFO_MAXSIZE = 4096
_MAXCOMLEN = 16


class DurableStateError(RuntimeError):
    reason_code = "DURABLE_STATE_CORRUPT"


class DurableStateCorrupt(DurableStateError):
    reason_code = "DURABLE_STATE_CORRUPT"


class DurableOwnerLost(DurableStateError):
    reason_code = "DURABLE_OWNER_LOST"


class DurableRecoveryOverCapacity(DurableStateError):
    reason_code = "DURABLE_RECOVERY_OVER_CAPACITY"


class DurableControlPending(DurableStateError):
    reason_code = "DURABLE_CONTROL_PENDING"


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * _MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * _MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True, slots=True)
class DurableSnapshot:
    state: dict[str, Any]
    chunks: tuple[tuple[str, bytes], ...]


def default_durable_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "Agent Runtime" / "durable-jobs"


def durable_job_id(start_identity: str) -> str:
    if not isinstance(start_identity, str) or _START_IDENTITY.fullmatch(start_identity) is None:
        raise ValueError("start_identity must be exactly 32 lowercase hexadecimal characters")
    return hashlib.sha256(
        b"agent-runtime.durable-job.v1\0" + start_identity.encode("ascii")
    ).hexdigest()


def execution_spec_digest(
    *,
    cwd: str,
    argv: list[str],
    hard_wall_seconds: float,
) -> str:
    hard_wall_ms = int(round(float(hard_wall_seconds) * 1000.0))
    if hard_wall_ms <= 0:
        raise ValueError("hard wall must be positive")
    payload = {
        "argv": list(argv),
        "cwd": cwd,
        "durability": "runtime_restart",
        "entry_surface": "terminal_start",
        "hard_wall_ms": hard_wall_ms,
        "mode": "pipe",
    }
    return hashlib.sha256(canonical_structured_bytes(payload)).hexdigest()


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _integrity_record(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("integrity", None)
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    payload["integrity"] = {
        "version": DURABLE_INTEGRITY_VERSION,
        "sha256": digest,
    }
    return payload


def _validate_integrity(value: dict[str, Any]) -> None:
    integrity = value.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {"version", "sha256"}:
        raise DurableStateCorrupt("durable state integrity metadata is invalid")
    if integrity.get("version") != DURABLE_INTEGRITY_VERSION:
        raise DurableStateCorrupt("durable state integrity version is unsupported")
    digest = integrity.get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise DurableStateCorrupt("durable state integrity digest is invalid")
    payload = dict(value)
    payload.pop("integrity", None)
    observed = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    if not secrets.compare_digest(digest, observed):
        raise DurableStateCorrupt("durable state integrity check failed")


def _validate_positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise DurableStateCorrupt(f"{field} is invalid")
    return float(value)


def _validate_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DurableStateCorrupt(f"{field} is invalid")
    return value


def _validate_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DurableStateCorrupt(f"{field} is invalid")
    return value


def _validate_process_identity(value: object, field: str) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DurableStateCorrupt(f"{field} is invalid")
    expected = {
        "pid",
        "pgid",
        "start_sec",
        "start_usec",
        "executable_dev",
        "executable_ino",
    }
    if set(value) != expected:
        raise DurableStateCorrupt(f"{field} shape is invalid")
    parsed: dict[str, int] = {}
    for name in expected:
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise DurableStateCorrupt(f"{field}.{name} is invalid")
        parsed[name] = raw
    if parsed["pid"] <= 0 or parsed["pgid"] <= 0:
        raise DurableStateCorrupt(f"{field} pid identity is invalid")
    return parsed


def validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DurableStateCorrupt("durable state is not an object")
    required = {
        "schema_version",
        "session_id",
        "start_identity",
        "spec_digest",
        "durability",
        "mode",
        "entry_surface",
        "lifecycle",
        "status",
        "runner_identity",
        "process_identity",
        "hard_wall_deadline_epoch",
        "journal_generation",
        "journal_sha256",
        "base_cursor",
        "retained_output_bytes",
        "dropped_output_bytes",
        "exit_code",
        "termination_reason",
        "created_at_epoch",
        "completed_at_epoch",
        "integrity",
    }
    if set(value) != required:
        raise DurableStateCorrupt("durable state shape is invalid")
    _validate_integrity(value)
    if value.get("schema_version") != DURABLE_STATE_SCHEMA_VERSION:
        raise DurableStateCorrupt("durable state schema is unsupported")
    if not isinstance(value.get("session_id"), str) or _SESSION_ID.fullmatch(value["session_id"]) is None:
        raise DurableStateCorrupt("durable session_id is invalid")
    if (
        not isinstance(value.get("start_identity"), str)
        or _START_IDENTITY.fullmatch(value["start_identity"]) is None
    ):
        raise DurableStateCorrupt("durable start_identity is invalid")
    if not isinstance(value.get("spec_digest"), str) or _SHA256.fullmatch(value["spec_digest"]) is None:
        raise DurableStateCorrupt("durable spec digest is invalid")
    if value.get("durability") != "runtime_restart" or value.get("mode") != "pipe":
        raise DurableStateCorrupt("durable execution mode is invalid")
    if value.get("entry_surface") != "terminal_start":
        raise DurableStateCorrupt("durable entry surface is invalid")
    if value.get("lifecycle") not in _ALLOWED_LIFECYCLES:
        raise DurableStateCorrupt("durable lifecycle is invalid")
    if value.get("status") not in _ALLOWED_STATUS:
        raise DurableStateCorrupt("durable status is invalid")
    runner = _validate_process_identity(value.get("runner_identity"), "runner_identity")
    process = _validate_process_identity(value.get("process_identity"), "process_identity")
    if runner is None:
        raise DurableStateCorrupt("runner identity is required")
    status = value["status"]
    lifecycle = value["lifecycle"]
    if status == "running" and (lifecycle != "RUNNING" or process is None):
        raise DurableStateCorrupt("running durable state lacks process ownership")
    if status == "starting" and lifecycle != "STARTING":
        raise DurableStateCorrupt("starting durable lifecycle is invalid")
    if status == "exited" and lifecycle not in {
        "COMPLETED",
        "START_FAILED_PRE_EFFECT",
        "START_FAILED_POST_EFFECT",
    }:
        raise DurableStateCorrupt("terminal durable lifecycle is invalid")
    _validate_positive_number(value.get("hard_wall_deadline_epoch"), "hard_wall_deadline_epoch")
    _validate_nonnegative_int(value.get("journal_generation"), "journal_generation")
    if not isinstance(value.get("journal_sha256"), str) or _SHA256.fullmatch(value["journal_sha256"]) is None:
        raise DurableStateCorrupt("journal digest is invalid")
    base_cursor = _validate_nonnegative_int(value.get("base_cursor"), "base_cursor")
    retained = _validate_nonnegative_int(value.get("retained_output_bytes"), "retained_output_bytes")
    dropped = _validate_nonnegative_int(value.get("dropped_output_bytes"), "dropped_output_bytes")
    if retained > MAX_RETAINED_OUTPUT_BYTES or dropped != base_cursor:
        raise DurableStateCorrupt("durable output accounting is invalid")
    _validate_optional_int(value.get("exit_code"), "exit_code")
    reason = value.get("termination_reason")
    if reason is not None and reason not in _ALLOWED_TERMINATION:
        raise DurableStateCorrupt("durable termination reason is invalid")
    if status == "exited" and reason is None:
        raise DurableStateCorrupt("terminal durable state lacks termination reason")
    if status != "exited" and (reason is not None or value.get("exit_code") is not None):
        raise DurableStateCorrupt("running durable state has terminal fields")
    _validate_positive_number(value.get("created_at_epoch"), "created_at_epoch")
    completed = value.get("completed_at_epoch")
    if status == "exited":
        _validate_positive_number(completed, "completed_at_epoch")
    elif completed is not None:
        raise DurableStateCorrupt("running durable state has completion time")
    return value


def _parse_json(raw: bytes, *, label: str) -> object:
    try:
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DurableStateCorrupt(f"{label} is malformed") from exc


def _libproc() -> ctypes.CDLL:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    return library


def observe_process_identity(pid: int) -> dict[str, int] | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        library = _libproc()
        info = _ProcBsdInfo()
        count = library.proc_pidinfo(
            pid,
            _PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if count != ctypes.sizeof(info) or int(info.pbi_pid) != pid:
            return None
        path_buffer = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
        path_count = library.proc_pidpath(pid, path_buffer, len(path_buffer))
        if path_count <= 0:
            return None
        executable = Path(os.fsdecode(path_buffer.value)).resolve(strict=True)
        observed = executable.stat()
    except (OSError, ValueError):
        return None
    return {
        "pid": pid,
        "pgid": int(info.pbi_pgid),
        "start_sec": int(info.pbi_start_tvsec),
        "start_usec": int(info.pbi_start_tvusec),
        "executable_dev": int(observed.st_dev),
        "executable_ino": int(observed.st_ino),
    }


def verify_process_identity(expected: dict[str, int] | None) -> bool:
    if expected is None:
        return False
    try:
        checked = _validate_process_identity(expected, "process_identity")
    except DurableStateCorrupt:
        return False
    if checked is None:
        return False
    observed = observe_process_identity(checked["pid"])
    return observed == checked


class DurableStore:
    def __init__(self, root: str | Path | None = None) -> None:
        chosen = default_durable_root() if root is None else Path(root)
        if not chosen.is_absolute():
            raise ValueError("durable state root must be absolute")
        self.root = chosen

    def _validate_directory(self, path: Path, mode: int, label: str) -> os.stat_result:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise DurableStateCorrupt(f"{label} is unavailable") from exc
        if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
            raise DurableStateCorrupt(f"{label} is not a real directory")
        if observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) != mode:
            raise DurableStateCorrupt(f"{label} ownership or mode is invalid")
        return observed

    def _ensure_root(self) -> None:
        if self.root.exists() or self.root.is_symlink():
            self._validate_directory(self.root, DURABLE_ROOT_MODE, "durable root")
            return
        self.root.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.root, DURABLE_ROOT_MODE)
        except FileExistsError:
            pass
        self._validate_directory(self.root, DURABLE_ROOT_MODE, "durable root")
        self._fsync_dir(self.root.parent)

    def _job_dir(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise DurableStateCorrupt("durable job id is invalid")
        return self.root / job_id

    def job_path_for_identity(self, start_identity: str) -> Path:
        return self._job_dir(durable_job_id(start_identity))

    def job_exists_for_identity(self, start_identity: str) -> bool:
        path = self.job_path_for_identity(start_identity)
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise DurableStateCorrupt("durable job path is unavailable") from exc
        return True

    def _validate_job_dir(self, job_id: str) -> Path:
        self._validate_directory(self.root, DURABLE_ROOT_MODE, "durable root")
        path = self._job_dir(job_id)
        self._validate_directory(path, DURABLE_JOB_MODE, "durable job directory")
        return path

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _secure_read(path: Path, *, maximum: int) -> bytes:
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise DurableStateCorrupt(f"durable file is unavailable: {path.name}") from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != DURABLE_FILE_MODE
            or before.st_nlink != 1
        ):
            raise DurableStateCorrupt(f"durable file metadata is invalid: {path.name}")
        if before.st_size > maximum:
            raise DurableStateCorrupt(f"durable file exceeds bound: {path.name}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise DurableStateCorrupt(f"durable file cannot be opened safely: {path.name}") from exc
        try:
            after = os.fstat(fd)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != os.getuid()
                or stat.S_IMODE(after.st_mode) != DURABLE_FILE_MODE
                or after.st_nlink != 1
                or after.st_size > maximum
            ):
                raise DurableStateCorrupt(f"durable file changed during validation: {path.name}")
            remaining = maximum + 1
            chunks: list[bytes] = []
            while remaining > 0:
                part = os.read(fd, min(65536, remaining))
                if not part:
                    break
                chunks.append(part)
                remaining -= len(part)
            raw = b"".join(chunks)
            if len(raw) > maximum:
                raise DurableStateCorrupt(f"durable file exceeds bound: {path.name}")
            final = os.fstat(fd)
            if final.st_size != len(raw) or final.st_dev != after.st_dev or final.st_ino != after.st_ino:
                raise DurableStateCorrupt(f"durable file changed during read: {path.name}")
            return raw
        finally:
            os.close(fd)

    @staticmethod
    def _validate_existing_file(path: Path) -> None:
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != DURABLE_FILE_MODE
            or observed.st_nlink != 1
        ):
            raise DurableStateCorrupt(f"durable destination metadata is invalid: {path.name}")

    def _atomic_write(self, path: Path, payload: bytes) -> str:
        if len(payload) > MAX_SPEC_BYTES:
            raise DurableStateCorrupt("durable atomic payload exceeds bound")
        self._validate_existing_file(path)
        parent = path.parent
        temp = parent / f".{path.name}.tmp-{secrets.token_hex(8)}"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(temp, flags, DURABLE_FILE_MODE)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short durable state write")
                view = view[written:]
            os.fsync(fd)
            observed = os.fstat(fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.getuid()
                or stat.S_IMODE(observed.st_mode) != DURABLE_FILE_MODE
                or observed.st_nlink != 1
            ):
                raise DurableStateCorrupt("durable temporary file metadata is invalid")
        finally:
            os.close(fd)
        try:
            self._validate_existing_file(path)
            os.replace(temp, path)
            self._fsync_dir(parent)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return hashlib.sha256(payload).hexdigest()

    def prepare_spec(self, spec: dict[str, Any]) -> str:
        start_identity = spec.get("start_identity")
        if not isinstance(start_identity, str) or _START_IDENTITY.fullmatch(start_identity) is None:
            raise DurableStateCorrupt("durable start identity is invalid")
        job_id = durable_job_id(start_identity)
        if spec.get("job_id") != job_id:
            raise DurableStateCorrupt("durable job id does not match identity")
        self._ensure_root()
        job_dir = self._job_dir(job_id)
        try:
            os.mkdir(job_dir, DURABLE_JOB_MODE)
        except FileExistsError as exc:
            raise DurableStateCorrupt("durable job identity already exists") from exc
        self._validate_directory(job_dir, DURABLE_JOB_MODE, "durable job directory")
        self._fsync_dir(self.root)
        raw = _canonical_json_bytes(spec)
        if len(raw) > MAX_SPEC_BYTES:
            raise DurableStateCorrupt("durable spec exceeds bound")
        self._atomic_write(job_dir / "spec.json", raw)
        return job_id

    def read_spec(self, job_id: str) -> dict[str, Any]:
        job_dir = self._validate_job_dir(job_id)
        raw = self._secure_read(job_dir / "spec.json", maximum=MAX_SPEC_BYTES)
        value = _parse_json(raw, label="durable spec")
        if not isinstance(value, dict):
            raise DurableStateCorrupt("durable spec is not an object")
        expected = {
            "schema_version",
            "job_id",
            "session_id",
            "start_identity",
            "spec_digest",
            "cwd",
            "argv",
            "mode",
            "durability",
            "entry_surface",
            "hard_wall_ms",
        }
        if set(value) != expected or value.get("schema_version") != DURABLE_SPEC_SCHEMA_VERSION:
            raise DurableStateCorrupt("durable spec shape is invalid")
        if value.get("job_id") != job_id:
            raise DurableStateCorrupt("durable spec job id is invalid")
        if (
            not isinstance(value.get("session_id"), str)
            or _SESSION_ID.fullmatch(value["session_id"]) is None
            or not isinstance(value.get("start_identity"), str)
            or _START_IDENTITY.fullmatch(value["start_identity"]) is None
            or durable_job_id(value["start_identity"]) != job_id
            or not isinstance(value.get("spec_digest"), str)
            or _SHA256.fullmatch(value["spec_digest"]) is None
            or not isinstance(value.get("cwd"), str)
            or not value["cwd"]
            or not isinstance(value.get("argv"), list)
            or not value["argv"]
            or any(not isinstance(item, str) for item in value["argv"])
            or value.get("mode") != "pipe"
            or value.get("durability") != "runtime_restart"
            or value.get("entry_surface") != "terminal_start"
            or isinstance(value.get("hard_wall_ms"), bool)
            or not isinstance(value.get("hard_wall_ms"), int)
            or value["hard_wall_ms"] <= 0
        ):
            raise DurableStateCorrupt("durable spec values are invalid")
        observed_digest = execution_spec_digest(
            cwd=value["cwd"],
            argv=list(value["argv"]),
            hard_wall_seconds=value["hard_wall_ms"] / 1000.0,
        )
        if not secrets.compare_digest(value["spec_digest"], observed_digest):
            raise DurableStateCorrupt("durable spec digest mismatch")
        return value

    def remove_spec(self, job_id: str) -> None:
        job_dir = self._validate_job_dir(job_id)
        path = job_dir / "spec.json"
        self._secure_read(path, maximum=MAX_SPEC_BYTES)
        os.unlink(path)
        self._fsync_dir(job_dir)

    def abort_pre_dispatch(self, job_id: str) -> None:
        job_dir = self._validate_job_dir(job_id)
        entries = {entry.name for entry in os.scandir(job_dir)}
        if entries - {"spec.json"}:
            raise DurableStateCorrupt("cannot remove durable job after dispatch state exists")
        if "spec.json" in entries:
            self._secure_read(job_dir / "spec.json", maximum=MAX_SPEC_BYTES)
            os.unlink(job_dir / "spec.json")
        os.rmdir(job_dir)
        self._fsync_dir(self.root)

    def write_snapshot(
        self,
        job_id: str,
        *,
        state: dict[str, Any],
        generation: int,
        base_cursor: int,
        chunks: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        job_dir = self._validate_job_dir(job_id)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise DurableStateCorrupt("journal generation is invalid")
        if isinstance(base_cursor, bool) or not isinstance(base_cursor, int) or base_cursor < 0:
            raise DurableStateCorrupt("journal base cursor is invalid")
        retained = 0
        encoded_chunks: list[dict[str, str]] = []
        for stream_name, data in chunks:
            if stream_name not in _ALLOWED_STREAMS or not isinstance(data, bytes):
                raise DurableStateCorrupt("journal chunk is invalid")
            retained += len(data)
            if retained > MAX_RETAINED_OUTPUT_BYTES:
                raise DurableStateCorrupt("journal retained output exceeds bound")
            encoded_chunks.append(
                {
                    "stream": stream_name,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                }
            )
        journal = {
            "schema_version": DURABLE_JOURNAL_SCHEMA_VERSION,
            "generation": generation,
            "base_cursor": base_cursor,
            "chunks": encoded_chunks,
        }
        journal_raw = _canonical_json_bytes(journal)
        if len(journal_raw) > MAX_JOURNAL_BYTES:
            raise DurableStateCorrupt("durable journal exceeds encoded bound")
        journal_sha = self._atomic_write(job_dir / "journal.json", journal_raw)

        persisted_state = dict(state)
        persisted_state["journal_generation"] = generation
        persisted_state["journal_sha256"] = journal_sha
        persisted_state["base_cursor"] = base_cursor
        persisted_state["retained_output_bytes"] = retained
        persisted_state["dropped_output_bytes"] = base_cursor
        persisted_state = _integrity_record(persisted_state)
        validate_state(persisted_state)
        state_raw = _canonical_json_bytes(persisted_state)
        if len(state_raw) > MAX_STATE_BYTES:
            raise DurableStateCorrupt("durable state exceeds bound")
        self._atomic_write(job_dir / "state.json", state_raw)
        return persisted_state

    @staticmethod
    def _parse_journal(raw: bytes) -> tuple[int, int, tuple[tuple[str, bytes], ...]]:
        value = _parse_json(raw, label="durable journal")
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "generation",
            "base_cursor",
            "chunks",
        }:
            raise DurableStateCorrupt("durable journal shape is invalid")
        if value.get("schema_version") != DURABLE_JOURNAL_SCHEMA_VERSION:
            raise DurableStateCorrupt("durable journal schema is unsupported")
        generation = _validate_nonnegative_int(value.get("generation"), "journal generation")
        base_cursor = _validate_nonnegative_int(value.get("base_cursor"), "journal base cursor")
        raw_chunks = value.get("chunks")
        if not isinstance(raw_chunks, list):
            raise DurableStateCorrupt("durable journal chunks are invalid")
        chunks: list[tuple[str, bytes]] = []
        retained = 0
        for item in raw_chunks:
            if (
                not isinstance(item, dict)
                or set(item) != {"stream", "data_b64"}
                or item.get("stream") not in _ALLOWED_STREAMS
                or not isinstance(item.get("data_b64"), str)
            ):
                raise DurableStateCorrupt("durable journal chunk shape is invalid")
            try:
                data = base64.b64decode(item["data_b64"], validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise DurableStateCorrupt("durable journal chunk encoding is invalid") from exc
            retained += len(data)
            if retained > MAX_RETAINED_OUTPUT_BYTES:
                raise DurableStateCorrupt("durable journal retained output exceeds bound")
            chunks.append((item["stream"], data))
        return generation, base_cursor, tuple(chunks)

    def read_snapshot(self, job_id: str) -> DurableSnapshot:
        job_dir = self._validate_job_dir(job_id)
        for attempt in range(_SNAPSHOT_READ_ATTEMPTS):
            state_raw = self._secure_read(job_dir / "state.json", maximum=MAX_STATE_BYTES)
            state = validate_state(_parse_json(state_raw, label="durable state"))
            journal_raw = self._secure_read(job_dir / "journal.json", maximum=MAX_JOURNAL_BYTES)
            consistent = secrets.compare_digest(
                hashlib.sha256(journal_raw).hexdigest(),
                state["journal_sha256"],
            )
            if consistent:
                generation, base_cursor, chunks = self._parse_journal(journal_raw)
                consistent = (
                    generation == state["journal_generation"]
                    and base_cursor == state["base_cursor"]
                    and sum(len(data) for _stream, data in chunks)
                    == state["retained_output_bytes"]
                )
                if consistent:
                    state_raw_after = self._secure_read(
                        job_dir / "state.json",
                        maximum=MAX_STATE_BYTES,
                    )
                    if state_raw_after == state_raw:
                        return DurableSnapshot(state=state, chunks=chunks)
            if attempt + 1 < _SNAPSHOT_READ_ATTEMPTS:
                time.sleep(_SNAPSHOT_RETRY_SECONDS)
        raise DurableStateCorrupt("durable state changed inconsistently during recovery")

    def read_for_identity(self, start_identity: str) -> DurableSnapshot:
        job_id = durable_job_id(start_identity)
        snapshot = self.read_snapshot(job_id)
        if snapshot.state["start_identity"] != start_identity:
            raise DurableStateCorrupt("durable identity binding is invalid")
        return snapshot

    def scan_job_ids(self) -> tuple[str, ...]:
        try:
            os.lstat(self.root)
        except FileNotFoundError:
            return ()
        self._validate_directory(self.root, DURABLE_ROOT_MODE, "durable root")
        entries = list(os.scandir(self.root))
        if len(entries) > DURABLE_SCAN_LIMIT:
            raise DurableRecoveryOverCapacity("durable state root exceeds bounded recovery scan")
        job_ids: list[str] = []
        for entry in entries:
            if _JOB_ID.fullmatch(entry.name) is None or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise DurableStateCorrupt("durable state root contains an unexpected entry")
            self._validate_directory(Path(entry.path), DURABLE_JOB_MODE, "durable job directory")
            job_ids.append(entry.name)
        return tuple(sorted(job_ids))

    def write_control(self, job_id: str, action: str) -> None:
        if action not in {"interrupt", "terminate"}:
            raise ValueError("durable control action must be interrupt or terminate")
        job_dir = self._validate_job_dir(job_id)
        path = job_dir / "control.json"
        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            self._secure_read(path, maximum=MAX_CONTROL_BYTES)
            raise DurableControlPending("a durable control request is already pending")
        payload = _canonical_json_bytes(
            {
                "schema_version": DURABLE_CONTROL_SCHEMA_VERSION,
                "action": action,
            }
        )
        self._atomic_write(path, payload)

    def take_control(self, job_id: str) -> str | None:
        job_dir = self._validate_job_dir(job_id)
        path = job_dir / "control.json"
        try:
            raw = self._secure_read(path, maximum=MAX_CONTROL_BYTES)
        except DurableStateCorrupt:
            try:
                os.lstat(path)
            except FileNotFoundError:
                return None
            raise
        value = _parse_json(raw, label="durable control")
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "action"}
            or value.get("schema_version") != DURABLE_CONTROL_SCHEMA_VERSION
            or value.get("action") not in {"interrupt", "terminate"}
        ):
            raise DurableStateCorrupt("durable control request is invalid")
        os.unlink(path)
        self._fsync_dir(job_dir)
        return value["action"]

    def remove_control(self, job_id: str) -> None:
        job_dir = self._validate_job_dir(job_id)
        path = job_dir / "control.json"
        try:
            self._secure_read(path, maximum=MAX_CONTROL_BYTES)
        except DurableStateCorrupt:
            try:
                os.lstat(path)
            except FileNotFoundError:
                return
            raise
        os.unlink(path)
        self._fsync_dir(job_dir)

    def remove_completed(self, job_id: str) -> bool:
        snapshot = self.read_snapshot(job_id)
        if snapshot.state["status"] != "exited":
            return False
        runner = snapshot.state["runner_identity"]
        process = snapshot.state["process_identity"]
        if verify_process_identity(runner) or verify_process_identity(process):
            return False
        job_dir = self._validate_job_dir(job_id)
        allowed = {"state.json", "journal.json"}
        names = {entry.name for entry in os.scandir(job_dir)}
        if names - allowed:
            raise DurableStateCorrupt("terminal durable job contains unexpected retained files")
        for name, maximum in (("state.json", MAX_STATE_BYTES), ("journal.json", MAX_JOURNAL_BYTES)):
            path = job_dir / name
            self._secure_read(path, maximum=maximum)
            os.unlink(path)
        os.rmdir(job_dir)
        self._fsync_dir(self.root)
        return True
