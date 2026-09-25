from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .durable_pipe import (
    DURABLE_ROOT_ENV,
    DURABLE_STATE_SCHEMA_VERSION,
    MAX_RETAINED_OUTPUT_BYTES,
    DurableStateError,
    DurableStore,
    observe_process_identity,
)
from .executor import (
    WORKSPACE_ROOT_ENV,
    _minimal_child_env,
    _terminate_process_group,
    _validated_argv,
    _validated_cwd,
    _workspace_root,
)
from .protection import _PROTECTED_GUARD


_READ_CHUNK_BYTES = 8192
_PERSIST_INTERVAL_SECONDS = 0.05
_READER_DRAIN_SECONDS = 0.5
_IDENTITY_WAIT_SECONDS = 0.25


class _OutputJournal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[tuple[str, bytes]] = []
        self._retained = 0
        self._base_cursor = 0
        self._generation = 0
        self._reader_count = 2
        self.readers_done = threading.Event()

    def append(self, stream: str, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._chunks.append((stream, data))
            self._retained += len(data)
            overflow = self._retained - MAX_RETAINED_OUTPUT_BYTES
            while overflow > 0 and self._chunks:
                first_stream, first = self._chunks[0]
                if len(first) <= overflow:
                    self._chunks.pop(0)
                    dropped = len(first)
                else:
                    self._chunks[0] = (first_stream, first[overflow:])
                    dropped = overflow
                self._base_cursor += dropped
                self._retained -= dropped
                overflow -= dropped
            self._generation += 1

    def reader_done(self) -> None:
        with self._lock:
            self._reader_count -= 1
            if self._reader_count <= 0:
                self.readers_done.set()

    def snapshot(self) -> tuple[int, int, list[tuple[str, bytes]]]:
        with self._lock:
            return self._generation, self._base_cursor, list(self._chunks)


def _read_stream(journal: _OutputJournal, name: str, stream: Any) -> None:
    try:
        while True:
            try:
                chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
            except InterruptedError:
                continue
            if not chunk:
                break
            journal.append(name, chunk)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass
        journal.reader_done()


def _identity_with_retry(pid: int) -> dict[str, int] | None:
    deadline = time.monotonic() + _IDENTITY_WAIT_SECONDS
    while True:
        identity = observe_process_identity(pid)
        if identity is not None:
            return identity
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _base_state(
    spec: dict[str, Any],
    *,
    runner_identity: dict[str, int],
    process_identity: dict[str, int] | None,
    hard_wall_deadline_epoch: float,
    created_at_epoch: float,
) -> dict[str, Any]:
    return {
        "schema_version": DURABLE_STATE_SCHEMA_VERSION,
        "session_id": spec["session_id"],
        "start_identity": spec["start_identity"],
        "spec_digest": spec["spec_digest"],
        "durability": "runtime_restart",
        "mode": "pipe",
        "entry_surface": "terminal_start",
        "lifecycle": "RUNNING",
        "status": "running",
        "runner_identity": runner_identity,
        "process_identity": process_identity,
        "hard_wall_deadline_epoch": hard_wall_deadline_epoch,
        "journal_generation": 0,
        "journal_sha256": "0" * 64,
        "base_cursor": 0,
        "retained_output_bytes": 0,
        "dropped_output_bytes": 0,
        "exit_code": None,
        "termination_reason": None,
        "created_at_epoch": created_at_epoch,
        "completed_at_epoch": None,
    }


def _persist(
    store: DurableStore,
    job_id: str,
    state: dict[str, Any],
    journal: _OutputJournal,
) -> int:
    generation, base_cursor, chunks = journal.snapshot()
    store.write_snapshot(
        job_id,
        state=state,
        generation=generation,
        base_cursor=base_cursor,
        chunks=chunks,
    )
    return generation


def _terminalize(
    state: dict[str, Any],
    *,
    lifecycle: str,
    termination_reason: str,
    exit_code: int | None,
) -> None:
    state["lifecycle"] = lifecycle
    state["status"] = "exited"
    state["termination_reason"] = termination_reason
    state["exit_code"] = exit_code
    state["completed_at_epoch"] = time.time()


def _write_pre_effect_failure(
    store: DurableStore,
    job_id: str,
    spec: dict[str, Any],
    runner_identity: dict[str, int],
) -> None:
    now = time.time()
    state = _base_state(
        spec,
        runner_identity=runner_identity,
        process_identity=None,
        hard_wall_deadline_epoch=now + (spec["hard_wall_ms"] / 1000.0),
        created_at_epoch=now,
    )
    _terminalize(
        state,
        lifecycle="START_FAILED_PRE_EFFECT",
        termination_reason="start_failed_pre_effect",
        exit_code=None,
    )
    journal = _OutputJournal()
    journal.reader_done()
    journal.reader_done()
    _persist(store, job_id, state, journal)
    store.remove_spec(job_id)


def _worker(job_id: str) -> int:
    root = os.environ.get(DURABLE_ROOT_ENV)
    if not root:
        return 2
    store = DurableStore(Path(root))
    try:
        spec = store.read_spec(job_id)
    except DurableStateError:
        return 2

    runner_identity = _identity_with_retry(os.getpid())
    if runner_identity is None:
        return 2

    process: subprocess.Popen[bytes] | None = None
    try:
        checked_argv = _validated_argv(list(spec["argv"]))
        _PROTECTED_GUARD.check(checked_argv, tool_name="terminal_start")
        checked_cwd = str(_validated_cwd(str(spec["cwd"]), _workspace_root()))
    except Exception:
        try:
            _write_pre_effect_failure(store, job_id, spec, runner_identity)
        except Exception:
            pass
        return 2

    created_at_epoch = time.time()
    hard_wall_seconds = spec["hard_wall_ms"] / 1000.0
    deadline_epoch = created_at_epoch + hard_wall_seconds
    try:
        process = subprocess.Popen(
            checked_argv,
            cwd=checked_cwd,
            env=_minimal_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        try:
            _write_pre_effect_failure(store, job_id, spec, runner_identity)
        except Exception:
            pass
        return 2

    process_identity = _identity_with_retry(process.pid)
    if process_identity is None and process.poll() is None:
        _terminate_process_group(process)
        return 2

    state = _base_state(
        spec,
        runner_identity=runner_identity,
        process_identity=process_identity,
        hard_wall_deadline_epoch=deadline_epoch,
        created_at_epoch=created_at_epoch,
    )
    journal = _OutputJournal()

    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        return 2

    stdout_thread = threading.Thread(
        target=_read_stream,
        args=(journal, "stdout", process.stdout),
        name=f"durable-stdout-{job_id[:8]}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(journal, "stderr", process.stderr),
        name=f"durable-stderr-{job_id[:8]}",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        persisted_generation = _persist(store, job_id, state, journal)
        store.remove_spec(job_id)
    except Exception:
        _terminate_process_group(process)
        return 2

    wake = threading.Event()

    def on_control(_signum: int, _frame: object) -> None:
        wake.set()

    signal.signal(signal.SIGUSR1, on_control)

    termination_reason = "natural_exit"
    while True:
        returncode = process.poll()
        if returncode is not None:
            termination_reason = "natural_exit"
            break

        now = time.time()
        if now >= deadline_epoch:
            current = observe_process_identity(process.pid)
            if process_identity is None or current != process_identity:
                return 2
            termination_reason = "hard_wall_timeout"
            _terminate_process_group(process)
            break

        action: str | None = None
        try:
            action = store.take_control(job_id)
        except DurableStateError:
            action = None
        if action == "interrupt":
            current = observe_process_identity(process.pid)
            if process_identity is not None and current == process_identity:
                try:
                    os.killpg(process_identity["pgid"], signal.SIGINT)
                except ProcessLookupError:
                    pass
        elif action == "terminate":
            current = observe_process_identity(process.pid)
            if process_identity is None or current != process_identity:
                return 2
            termination_reason = "explicit_terminate"
            _terminate_process_group(process)
            break

        generation, _base, _chunks = journal.snapshot()
        if generation != persisted_generation:
            try:
                persisted_generation = _persist(store, job_id, state, journal)
            except Exception:
                _terminate_process_group(process)
                return 2

        wake.wait(_PERSIST_INTERVAL_SECONDS)
        wake.clear()

    original_returncode = process.poll()
    if termination_reason == "natural_exit":
        try:
            _terminate_process_group(process)
        except Exception:
            termination_reason = "start_failed_post_effect"
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
    journal.readers_done.wait(_READER_DRAIN_SECONDS)

    if not journal.readers_done.is_set():
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        journal.readers_done.wait(_READER_DRAIN_SECONDS)

    exit_code = original_returncode if original_returncode is not None else process.returncode
    lifecycle = "COMPLETED"
    if termination_reason == "start_failed_post_effect":
        lifecycle = "START_FAILED_POST_EFFECT"
    _terminalize(
        state,
        lifecycle=lifecycle,
        termination_reason=termination_reason,
        exit_code=exit_code,
    )
    try:
        _persist(store, job_id, state, journal)
    except Exception:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    job_id = args[0]

    try:
        child = os.fork()
    except OSError:
        return 2
    if child > 0:
        return 0

    try:
        os.setsid()
    except OSError:
        os._exit(2)

    try:
        code = _worker(job_id)
    except BaseException:
        code = 2
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
