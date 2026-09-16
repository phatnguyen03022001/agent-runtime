from __future__ import annotations

import ctypes
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RuntimeCapacityError, RuntimeValidationError

MAX_PARALLELISM_ENV = "AGENT_RUNTIME_MAX_PARALLELISM"
DEFAULT_MAX_PARALLELISM = 2
HARD_EXECUTION_CEILING = 6
V2_EVIDENCE_CEILING = HARD_EXECUTION_CEILING
HEAVY_CAPACITY_ERROR = "terminal execution capacity exhausted"
SAMPLE_WINDOW_SECONDS = 0.05
SAMPLE_WINDOW_MS = 50
MIN_MEMORY_HEADROOM_BYTES = 1024**3
MIN_DISK_AVAILABLE_BYTES = 5 * 1024**3

_HOST_CPU_LOAD_INFO = 3
_HOST_VM_INFO64 = 4
_CPU_STATE_IDLE = 2


@dataclass(frozen=True)
class CapacitySignals:
    active_processors: int
    load1: float
    cpu_busy_fraction: float
    thermal_state: str
    swap_total_bytes: int
    swap_used_bytes: int
    swapin_delta_pages: int
    swapout_delta_pages: int
    vm_free_bytes: int
    vm_inactive_bytes: int
    vm_purgeable_bytes: int
    vm_compressor_bytes: int
    disk_available_bytes: int
    sampled_window_ms: int


class _HostCPULoadInfo(ctypes.Structure):
    _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]


class _VMStatistics64Prefix(ctypes.Structure):
    _fields_ = [
        ("free_count", ctypes.c_uint32),
        ("active_count", ctypes.c_uint32),
        ("inactive_count", ctypes.c_uint32),
        ("wire_count", ctypes.c_uint32),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", ctypes.c_uint32),
        ("speculative_count", ctypes.c_uint32),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", ctypes.c_uint32),
        ("throttled_count", ctypes.c_uint32),
        ("external_page_count", ctypes.c_uint32),
        ("internal_page_count", ctypes.c_uint32),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


class _SwapUsage(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("avail", ctypes.c_uint64),
        ("used", ctypes.c_uint64),
        ("pagesize", ctypes.c_uint32),
        ("encrypted", ctypes.c_int32),
    ]


class _FSID(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class _StatFS(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", _FSID),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _configured_max_parallelism() -> int:
    raw = os.environ.get(MAX_PARALLELISM_ENV)
    if raw is None:
        return DEFAULT_MAX_PARALLELISM
    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
        raise RuntimeValidationError(f"{MAX_PARALLELISM_ENV} must be an integer from 1 through 10")
    value = int(raw)
    if not 1 <= value <= 10:
        raise RuntimeValidationError(f"{MAX_PARALLELISM_ENV} must be an integer from 1 through 10")
    return value


def hard_execution_limit() -> int:
    """Return the immutable-at-runtime process-local execution ceiling.

    The operator setting may lower the ceiling, but never increase it beyond
    the supported x6 envelope.  Capacity observation is deliberately not part
    of this decision: it remains advisory and has no admission ownership.
    """

    return min(_configured_max_parallelism(), HARD_EXECUTION_CEILING)


class HeavyExecutionLease:
    """One idempotently releasable ownership token for a heavy workload."""

    def __init__(self, admission: "HeavyExecutionAdmission") -> None:
        self._admission = admission
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._admission._release()
        return True


class HeavyExecutionAdmission:
    """A fail-fast process-local admission boundary for terminal roots."""

    def __init__(self, limit: int | None = None) -> None:
        configured = hard_execution_limit() if limit is None else limit
        if isinstance(configured, bool) or not isinstance(configured, int) or not 1 <= configured <= HARD_EXECUTION_CEILING:
            raise RuntimeValidationError(
                f"heavy execution limit must be an integer from 1 through {HARD_EXECUTION_CEILING}"
            )
        self.limit = configured
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self) -> HeavyExecutionLease:
        with self._lock:
            if self._active >= self.limit:
                raise RuntimeCapacityError(HEAVY_CAPACITY_ERROR)
            self._active += 1
        return HeavyExecutionLease(self)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def _release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("heavy execution admission underflow")
            self._active -= 1


_HEAVY_EXECUTION_ADMISSION = HeavyExecutionAdmission()


def heavy_execution_admission() -> HeavyExecutionAdmission:
    """Return the single Runtime-owned heavy execution admission boundary."""

    return _HEAVY_EXECUTION_ADMISSION


def _libsystem() -> ctypes.CDLL:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.mach_host_self.restype = ctypes.c_uint32
    library.host_statistics.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_uint32)]
    library.host_statistics.restype = ctypes.c_int
    library.host_statistics64.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_uint32)]
    library.host_statistics64.restype = ctypes.c_int
    library.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    library.sysctlbyname.restype = ctypes.c_int
    library.statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFS)]
    library.statfs.restype = ctypes.c_int
    return library


def _mach_cpu_sample(library: ctypes.CDLL, host: int) -> tuple[int, int, int, int]:
    sample = _HostCPULoadInfo()
    count = ctypes.c_uint32(ctypes.sizeof(sample) // ctypes.sizeof(ctypes.c_int32))
    result = library.host_statistics(host, _HOST_CPU_LOAD_INFO, ctypes.cast(ctypes.byref(sample), ctypes.POINTER(ctypes.c_int32)), ctypes.byref(count))
    if result != 0 or count.value < 4:
        raise OSError(f"host_statistics failed with kern_return_t={result}")
    return tuple(int(value) for value in sample.cpu_ticks)


def _mach_vm_sample(library: ctypes.CDLL, host: int) -> _VMStatistics64Prefix:
    sample = _VMStatistics64Prefix()
    expected_count = ctypes.sizeof(sample) // ctypes.sizeof(ctypes.c_int32)
    count = ctypes.c_uint32(expected_count)
    result = library.host_statistics64(host, _HOST_VM_INFO64, ctypes.cast(ctypes.byref(sample), ctypes.POINTER(ctypes.c_int32)), ctypes.byref(count))
    if result != 0 or count.value < expected_count:
        raise OSError(f"host_statistics64 failed with kern_return_t={result}")
    return sample


def _swap_usage(library: ctypes.CDLL) -> _SwapUsage:
    usage = _SwapUsage()
    size = ctypes.c_size_t(ctypes.sizeof(usage))
    result = library.sysctlbyname(b"vm.swapusage", ctypes.byref(usage), ctypes.byref(size), None, 0)
    if result != 0 or size.value < ctypes.sizeof(usage):
        raise OSError(ctypes.get_errno(), "sysctlbyname(vm.swapusage) failed")
    return usage


def _disk_available_bytes(path: str | os.PathLike[str]) -> int:
    filesystem = _StatFS()
    library = _libsystem()
    if library.statfs(os.fsencode(path), ctypes.byref(filesystem)) != 0:
        raise OSError(ctypes.get_errno(), "statfs failed")
    return int(filesystem.f_bavail) * int(filesystem.f_bsize)


def _process_info() -> tuple[int, str]:
    ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    if address is None:
        raise OSError("objc_msgSend is unavailable")
    send_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
    send_uint = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(address)
    cls = objc.objc_getClass(b"NSProcessInfo")
    if not cls:
        raise OSError("NSProcessInfo is unavailable")
    info = send_object(cls, objc.sel_registerName(b"processInfo"))
    if not info:
        raise OSError("NSProcessInfo.processInfo is unavailable")
    active = int(send_uint(info, objc.sel_registerName(b"activeProcessorCount")))
    thermal_raw = int(send_uint(info, objc.sel_registerName(b"thermalState")))
    if active < 1:
        raise OSError("activeProcessorCount is invalid")
    return active, {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}.get(thermal_raw, "unknown")


def _counter_delta(after: int, before: int, *, bits: int | None = None) -> int:
    if bits is not None:
        return (after - before) & ((1 << bits) - 1)
    if after < before:
        raise OSError("monotonic VM counter moved backwards")
    return after - before


def _collect_signals() -> CapacitySignals:
    library = _libsystem()
    host = int(library.mach_host_self())
    cpu_before = _mach_cpu_sample(library, host)
    vm_before = _mach_vm_sample(library, host)
    time.sleep(SAMPLE_WINDOW_SECONDS)
    cpu_after = _mach_cpu_sample(library, host)
    vm_after = _mach_vm_sample(library, host)

    deltas = [_counter_delta(after, before, bits=32) for before, after in zip(cpu_before, cpu_after)]
    total_ticks = sum(deltas)
    if total_ticks <= 0:
        raise OSError("CPU sampling window produced no ticks")
    busy_fraction = 1.0 - (deltas[_CPU_STATE_IDLE] / total_ticks)

    active_processors, thermal_state = _process_info()
    load1 = float(os.getloadavg()[0])
    if not math.isfinite(load1) or load1 < 0:
        raise OSError("load average is invalid")
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    if page_size <= 0:
        raise OSError("page size is invalid")
    swap = _swap_usage(library)

    workspace_raw = os.environ.get("AGENT_RUNTIME_WORKSPACE_ROOT", "")
    workspace = Path(workspace_raw)
    if not workspace_raw or not workspace.is_absolute() or not workspace.is_dir():
        raise OSError("AGENT_RUNTIME_WORKSPACE_ROOT is unavailable")
    disk_available_bytes = _disk_available_bytes(workspace)

    return CapacitySignals(
        active_processors=active_processors,
        load1=load1,
        cpu_busy_fraction=max(0.0, min(1.0, busy_fraction)),
        thermal_state=thermal_state,
        swap_total_bytes=int(swap.total),
        swap_used_bytes=int(swap.used),
        swapin_delta_pages=_counter_delta(int(vm_after.swapins), int(vm_before.swapins)),
        swapout_delta_pages=_counter_delta(int(vm_after.swapouts), int(vm_before.swapouts)),
        vm_free_bytes=int(vm_after.free_count) * page_size,
        vm_inactive_bytes=int(vm_after.inactive_count) * page_size,
        vm_purgeable_bytes=int(vm_after.purgeable_count) * page_size,
        vm_compressor_bytes=int(vm_after.compressor_page_count) * page_size,
        disk_available_bytes=disk_available_bytes,
        sampled_window_ms=SAMPLE_WINDOW_MS,
    )


def _signal_summary(signals: CapacitySignals) -> dict[str, Any]:
    return {
        "active_processors": signals.active_processors,
        "load1": round(signals.load1, 3),
        "cpu_busy_pct": round(signals.cpu_busy_fraction * 100.0, 1),
        "sampled_window_ms": signals.sampled_window_ms,
        "thermal_state": signals.thermal_state,
        "swap_used_bytes": signals.swap_used_bytes,
        "swap_total_bytes": signals.swap_total_bytes,
        "swapin_delta_pages": signals.swapin_delta_pages,
        "swapout_delta_pages": signals.swapout_delta_pages,
        "vm_free_bytes": signals.vm_free_bytes,
        "vm_inactive_bytes": signals.vm_inactive_bytes,
        "vm_purgeable_bytes": signals.vm_purgeable_bytes,
        "vm_compressor_bytes": signals.vm_compressor_bytes,
        "disk_available_bytes": signals.disk_available_bytes,
    }


def _evaluate(signals: CapacitySignals, operator_max: int) -> dict[str, Any]:
    evidence_reasons: list[str] = []
    if signals.active_processors < 2:
        evidence_reasons.append("LIMIT_CPU_HEADROOM")
    else:
        max_busy_fraction = 1.0 - (2.0 / signals.active_processors)
        if not 0.0 <= signals.cpu_busy_fraction <= max_busy_fraction:
            evidence_reasons.append("LIMIT_CPU_HEADROOM")
    if not math.isfinite(signals.load1) or signals.load1 >= signals.active_processors:
        evidence_reasons.append("LIMIT_SUSTAINED_LOAD")
    if signals.thermal_state != "nominal":
        evidence_reasons.append("LIMIT_THERMAL")
    if signals.swapout_delta_pages > 0:
        evidence_reasons.append("LIMIT_SWAP_ACTIVITY")
    if signals.vm_free_bytes + signals.vm_inactive_bytes < MIN_MEMORY_HEADROOM_BYTES:
        evidence_reasons.append("LIMIT_MEMORY_HEADROOM")
    if signals.disk_available_bytes < MIN_DISK_AVAILABLE_BYTES:
        evidence_reasons.append("LIMIT_DISK_HEADROOM")

    if evidence_reasons:
        evidence_ceiling = 1
        reasons = evidence_reasons
    else:
        evidence_ceiling = V2_EVIDENCE_CEILING
        reasons = ["CAPACITY_X6_AVAILABLE", "LIMIT_V2_MAX_6"]
    if operator_max < evidence_ceiling:
        reasons = ["LIMIT_OPERATOR_MAX", *reasons]

    return {
        "schema_version": 1,
        "capacity_parallelism_ceiling": min(operator_max, evidence_ceiling),
        "reason_codes": reasons,
        "signals": _signal_summary(signals),
    }


def observe_capacity() -> dict[str, Any]:
    operator_max = _configured_max_parallelism()
    try:
        signals = _collect_signals()
    except Exception:
        return {
            "schema_version": 1,
            "capacity_parallelism_ceiling": 1,
            "reason_codes": ["LIMIT_SIGNAL_UNKNOWN"],
            "signals": {"probe_status": "unavailable", "sampled_window_ms": SAMPLE_WINDOW_MS},
        }
    return _evaluate(signals, operator_max)
