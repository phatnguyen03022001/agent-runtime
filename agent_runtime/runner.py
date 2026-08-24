from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigError, ProjectProfile, RuntimeConfig, load_config
from .git_ops import GitError, inspect_repository, sync_checkout
from .state import (
    ACTIVE_STATES,
    MAX_VERIFY_LOG_BYTES,
    TERMINAL_STATES,
    STATE_VERSION,
    StateStore,
)

VERIFY_TERMINATION_GRACE_SECONDS = 5.0
VERIFY_LOG_READ_CHUNK_BYTES = 64 * 1024
VERIFY_LOG_LIMIT_MARKER = b"\nVERIFY LOG LIMIT EXCEEDED\n"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _verifier_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath)}


def _drain_verify_output(
    pipe: Any,
    log_handle: Any,
    store: StateStore,
    overflow_event: threading.Event,
    write_error_event: threading.Event,
) -> None:
    discard = False
    try:
        while True:
            chunk = pipe.read(VERIFY_LOG_READ_CHUNK_BYTES)
            if not chunk:
                break
            if discard:
                continue
            try:
                written = store.append_log_bytes(
                    log_handle,
                    chunk,
                    reserve_bytes=len(VERIFY_LOG_LIMIT_MARKER),
                )
            except (OSError, ValueError):
                write_error_event.set()
                discard = True
                continue
            if written < len(chunk):
                overflow_event.set()
                discard = True
    except OSError:
        write_error_event.set()
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _join_verify_reader(reader: threading.Thread, pipe: Any) -> bool:
    reader.join(timeout=VERIFY_TERMINATION_GRACE_SECONDS)
    if reader.is_alive():
        try:
            pipe.close()
        except Exception:
            pass
        reader.join(timeout=VERIFY_TERMINATION_GRACE_SECONDS)
    return not reader.is_alive()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _elapsed_seconds(started_at: Any, finished_at: str | None = None) -> float | None:
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return round(max(0.0, (finished - started).total_seconds()), 2)


def _state_template(
    *,
    project: str,
    run_id: str,
    status: str,
    head: str,
    started_at: str,
    timeout_seconds: int,
    launcher_pid: int | None,
) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "project": project,
        "run_id": run_id,
        "status": status,
        "head": head,
        "launcher_pid": launcher_pid,
        "worker_pid": None,
        "verify_pid": None,
        "verify_pgid": None,
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "timed_out": False,
        "verification_ok": None,
        "failure_kind": None,
        "working_tree_after": None,
        "postconditions": {
            "same_head": None,
            "configured_branch": None,
            "clean": None,
            "in_sync": None,
            "remote_identity": None,
        },
        "log_run_id": None,
        "log_finalized": False,
    }


def _postconditions(profile: ProjectProfile, expected_head: str) -> tuple[dict[str, Any], dict[str, bool]]:
    after = inspect_repository(profile)
    if not after.get("ok"):
        return after, {
            "same_head": False,
            "configured_branch": False,
            "clean": False,
            "in_sync": False,
            "remote_identity": False,
        }
    return after, {
        "same_head": after.get("head") == expected_head,
        "configured_branch": after.get("current_branch") == profile.branch,
        "clean": after.get("clean") is True,
        "in_sync": after.get("in_sync") is True,
        "remote_identity": after.get("remote_identity_ok") is True,
    }


def _terminal_state(
    *,
    base_state: dict[str, Any],
    profile: ProjectProfile,
    expected_head: str,
    exit_code: int | None,
    timed_out: bool,
    failure_kind: str | None = None,
) -> dict[str, Any]:
    after, post = _postconditions(profile, expected_head)
    verification_ok = bool(
        exit_code == 0
        and not timed_out
        and failure_kind is None
        and all(post.values())
    )
    if timed_out:
        status = "TIMEOUT"
        failure_kind = failure_kind or "verify_timeout"
        verification_ok = False
    elif exit_code is None or exit_code != 0:
        status = "FAIL"
        failure_kind = failure_kind or "verify_exit_nonzero"
        verification_ok = False
    elif not verification_ok:
        status = "FAIL"
        failure_kind = failure_kind or "postcondition_failed"
    else:
        status = "PASS"
        failure_kind = None

    finished_at = _utc_now()
    terminal = dict(base_state)
    terminal.update(
        {
            "version": STATE_VERSION,
            "status": status,
            "finished_at": finished_at,
            "duration_seconds": _elapsed_seconds(base_state.get("started_at"), finished_at),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "verification_ok": verification_ok,
            "failure_kind": failure_kind,
            "working_tree_after": after,
            "postconditions": post,
        }
    )
    return terminal


def _try_acquire_lock(store: StateStore) -> int | None:
    store.ensure_directory()
    fd = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return fd


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _lock_busy(store: StateStore) -> tuple[bool | None, str | None]:
    try:
        fd = _try_acquire_lock(store)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if fd is None:
        return True, None
    _close_fd(fd)
    return False, None


def _validate_inherited_lock_fd(store: StateStore, lock_fd: int) -> None:
    inherited = os.fstat(lock_fd)
    expected = os.stat(store.lock_path)
    if inherited.st_dev != expected.st_dev or inherited.st_ino != expected.st_ino:
        raise RuntimeError("Inherited lock fd does not reference the project runner.lock.")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _recover_stale_locked(store: StateStore, profile: ProjectProfile) -> dict[str, Any] | None:
    state, state_error = store.read_state()
    recorded_status = state.get("status") if isinstance(state, dict) else None
    active = recorded_status in ACTIVE_STATES
    inprogress = store.inprogress_log_path.exists()
    orphan_log = inprogress and not active
    corrupt = state_error is not None
    if not (active or orphan_log or corrupt):
        return None

    if inprogress:
        store.finalize_log()

    prior_run_id = state.get("run_id") if isinstance(state, dict) else None
    run_id = prior_run_id if isinstance(prior_run_id, str) and prior_run_id else f"recovered-{uuid.uuid4().hex}"
    expected_head = state.get("head") if isinstance(state, dict) and isinstance(state.get("head"), str) else None
    if expected_head is not None:
        after, post = _postconditions(profile, expected_head)
    else:
        after = inspect_repository(profile)
        post = {
            "same_head": False,
            "configured_branch": after.get("current_branch") == profile.branch if after.get("ok") else False,
            "clean": after.get("clean") is True if after.get("ok") else False,
            "in_sync": after.get("in_sync") is True if after.get("ok") else False,
            "remote_identity": after.get("remote_identity_ok") is True if after.get("ok") else False,
        }

    recovered = dict(state) if isinstance(state, dict) else {}
    recovered.update(
        {
            "version": STATE_VERSION,
            "project": profile.project_id,
            "run_id": run_id,
            "status": "INTERRUPTED",
            "finished_at": _utc_now(),
            "duration_seconds": _elapsed_seconds(recovered.get("started_at")),
            "exit_code": None,
            "timed_out": False,
            "verification_ok": False,
            "failure_kind": (
                "state_corrupt"
                if corrupt
                else "orphan_inprogress_recovered"
                if orphan_log
                else "interrupted_without_terminal_state"
            ),
            "working_tree_after": after,
            "postconditions": post,
            "log_run_id": run_id if inprogress else None,
            "log_finalized": bool(inprogress),
        }
    )
    if state_error is not None:
        recovered["recovery_detail"] = state_error[-1000:]
    store.write_state(recovered)
    return {
        "run_id": run_id,
        "status": "INTERRUPTED",
        "failure_kind": recovered["failure_kind"],
    }


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_group(process: subprocess.Popen[Any], pgid: int) -> bool:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + VERIFY_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(pgid):
            break
        time.sleep(0.05)
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + VERIFY_TERMINATION_GRACE_SECONDS
        while time.monotonic() < kill_deadline:
            process.poll()
            if not _process_group_exists(pgid):
                break
            time.sleep(0.05)
    try:
        process.wait(timeout=VERIFY_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    return not _process_group_exists(pgid)


def _reap_detached_worker(process: subprocess.Popen[Any]) -> None:
    try:
        process.wait()
    except Exception:
        pass


class AgentRuntime:
    def __init__(self, config_path: str | Path):
        self.config: RuntimeConfig = load_config(config_path)

    def _profile(self, project: str) -> ProjectProfile:
        return self.config.resolve(project)

    def _store(self, project: str) -> StateStore:
        profile = self._profile(project)
        return StateStore(self.config.state_dir, profile.project_id)

    def get_head(self, project: str) -> dict[str, Any]:
        try:
            profile = self._profile(project)
        except ConfigError:
            return {"ok": False, "error": "Unknown project ID."}
        store = StateStore(self.config.state_dir, profile.project_id)
        state = inspect_repository(profile)
        busy, lock_error = _lock_busy(store)
        result = {
            "project": profile.project_id,
            "repository": profile.repository,
            "configured_branch": profile.branch,
            "current_branch": state.get("current_branch"),
            "head": state.get("head"),
            "clean": state.get("clean"),
            "cached_remote_head": state.get("cached_remote_head"),
            "in_sync": state.get("in_sync"),
            "remote_identity_ok": state.get("remote_identity_ok"),
            "busy": busy,
            "ok": state.get("ok") is True and lock_error is None,
        }
        if state.get("ok") is not True:
            result["error"] = state.get("error", "Repository inspection failed.")
        elif lock_error:
            result["error"] = "Runtime state unavailable."
        return result

    def sync(self, project: str) -> dict[str, Any]:
        try:
            profile = self._profile(project)
        except ConfigError:
            return {"ok": False, "busy": False, "error": "Unknown project ID."}
        store = StateStore(self.config.state_dir, profile.project_id)
        try:
            lock_fd = _try_acquire_lock(store)
        except OSError:
            return {"ok": False, "busy": False, "error": "Runtime state unavailable."}
        if lock_fd is None:
            return {"ok": False, "busy": True, "error": "Project is busy with an active operation."}
        try:
            try:
                recovery = _recover_stale_locked(store, profile)
                state = sync_checkout(profile)
            except (GitError, OSError, ValueError):
                return {"ok": False, "busy": False, "error": "Repository synchronization failed."}
            result = {
                "ok": True,
                "busy": False,
                "project": profile.project_id,
                "repository": profile.repository,
                "configured_branch": profile.branch,
                "head": state.get("head"),
                "clean": state.get("clean"),
                "cached_remote_head": state.get("cached_remote_head"),
                "in_sync": state.get("in_sync"),
                "remote_identity_ok": state.get("remote_identity_ok"),
            }
            if recovery is not None:
                result["recovered_verification"] = recovery
            return result
        finally:
            _close_fd(lock_fd)

    def run_verify(self, project: str) -> dict[str, Any]:
        try:
            profile = self._profile(project)
        except ConfigError:
            return {"ok": False, "accepted": False, "busy": False, "error": "Unknown project ID."}
        store = StateStore(self.config.state_dir, profile.project_id)
        try:
            lock_fd = _try_acquire_lock(store)
        except OSError:
            return {"ok": False, "accepted": False, "busy": False, "error": "Runtime state unavailable."}
        if lock_fd is None:
            return {"ok": False, "accepted": False, "busy": True, "error": "Project is busy with an active operation."}

        started_monotonic = time.monotonic()
        try:
            try:
                recovery = _recover_stale_locked(store, profile)
            except (OSError, ValueError):
                return {"ok": False, "accepted": False, "busy": False, "error": "Unable to recover stale verification state."}

            before = inspect_repository(profile)
            if not before.get("ok"):
                return {
                    "ok": False,
                    "accepted": False,
                    "busy": False,
                    "error": before.get("error", "Repository inspection failed."),
                }
            if before.get("current_branch") != profile.branch:
                return {"ok": False, "accepted": False, "busy": False, "error": "Checkout is on the wrong configured branch."}
            if before.get("clean") is not True:
                return {"ok": False, "accepted": False, "busy": False, "error": "Checkout working tree is dirty."}
            if before.get("in_sync") is not True:
                return {"ok": False, "accepted": False, "busy": False, "error": "Checkout is not synchronized with the cached configured remote branch."}
            if before.get("remote_identity_ok") is not True:
                return {"ok": False, "accepted": False, "busy": False, "error": "Configured remote identity mismatch."}

            run_id = uuid.uuid4().hex
            started_at = _utc_now()
            head = str(before["head"])
            starting = _state_template(
                project=profile.project_id,
                run_id=run_id,
                status="STARTING",
                head=head,
                started_at=started_at,
                timeout_seconds=profile.timeout_seconds,
                launcher_pid=os.getpid(),
            )
            try:
                store.write_state(starting)
                store.prepare_log()
                starting["log_run_id"] = run_id
                store.write_state(starting)
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "accepted": False,
                    "busy": False,
                    "status": "LAUNCH_FAILED",
                    "run_id": run_id,
                    "head": head,
                    "verification_ok": False,
                    "failure_kind": "state_or_log_prepare_failed",
                    "error": "Unable to initialize verification state.",
                }

            try:
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "agent_runtime.runner",
                        "--verify-worker",
                        "--config",
                        str(self.config.path),
                        "--project",
                        profile.project_id,
                        "--lock-fd",
                        str(lock_fd),
                        "--run-id",
                        run_id,
                        "--head",
                        head,
                        "--started-at",
                        started_at,
                    ],
                    cwd=PACKAGE_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=os.environ.copy(),
                    close_fds=True,
                    pass_fds=(lock_fd,),
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                try:
                    store.append_wrapper_log(f"\nVERIFY WORKER LAUNCH FAILED: {type(exc).__name__}: {exc}\n")
                    terminal = _terminal_state(
                        base_state=starting,
                        profile=profile,
                        expected_head=head,
                        exit_code=None,
                        timed_out=False,
                        failure_kind="worker_launch_failed",
                    )
                    terminal["status"] = "LAUNCH_FAILED"
                    terminal["verification_ok"] = False
                    store.commit_terminal_with_log(terminal)
                except (OSError, ValueError):
                    pass
                return {"ok": False, "accepted": False, "busy": False, "status": "LAUNCH_FAILED", "error": "Unable to launch verification worker."}

            try:
                threading.Thread(
                    target=_reap_detached_worker,
                    args=(worker,),
                    name="verify-worker-reaper",
                    daemon=True,
                ).start()
            except RuntimeError:
                pass

            _close_fd(lock_fd)
            lock_fd = -1
            result = {
                "ok": True,
                "accepted": True,
                "busy": False,
                "project": profile.project_id,
                "run_id": run_id,
                "status": "STARTING",
                "head": head,
                "worker_pid": worker.pid,
                "timeout_seconds": profile.timeout_seconds,
                "started_at": started_at,
                "verification_ok": None,
                "launch_duration_seconds": round(time.monotonic() - started_monotonic, 3),
            }
            if recovery is not None:
                result["recovered_verification"] = recovery
            return result
        finally:
            if lock_fd >= 0:
                _close_fd(lock_fd)

    def get_last_log(self, project: str) -> dict[str, Any]:
        try:
            profile = self._profile(project)
        except ConfigError:
            return {"ok": False, "error": "Unknown project ID.", "status": "UNKNOWN", "verification_ok": False}
        store = StateStore(self.config.state_dir, profile.project_id)
        state, state_error = store.read_state()
        recorded = state.get("status") if state else None
        effective = recorded
        verification_ok = state.get("verification_ok") if state else (False if state_error else None)
        busy: bool | None = None
        lock_error: str | None = None
        if recorded in ACTIVE_STATES:
            busy, lock_error = _lock_busy(store)
            if busy is False:
                effective = "INTERRUPTED"
                verification_ok = False

        log_result: dict[str, Any] | None = None
        required_pass_log_unreadable = False
        if state is not None and state.get("log_run_id") == state.get("run_id"):
            if recorded in ACTIVE_STATES and state.get("log_finalized") is not True:
                path = store.inprogress_log_path
            elif recorded in TERMINAL_STATES and state.get("log_finalized") is True:
                path = store.log_path
            else:
                path = None
            if path is not None:
                try:
                    log_result = store.read_log_tail(path)
                except FileNotFoundError:
                    if recorded == "PASS":
                        required_pass_log_unreadable = True
                except OSError as exc:
                    if recorded == "PASS":
                        required_pass_log_unreadable = True
                    lock_error = lock_error or f"Unable to read log: {type(exc).__name__}: {exc}"
        elif state is None:
            for path in (store.inprogress_log_path, store.log_path):
                try:
                    log_result = store.read_log_tail(path)
                    log_result["log_associated"] = False
                    break
                except FileNotFoundError:
                    continue
                except OSError:
                    continue

        if recorded == "PASS" and log_result is None:
            required_pass_log_unreadable = True
        if required_pass_log_unreadable:
            effective = "INTERRUPTED"
            verification_ok = False

        result: dict[str, Any] = {
            "ok": state_error is None and lock_error is None and not required_pass_log_unreadable,
            "project": profile.project_id,
            "status": effective or "UNKNOWN",
            "verification_ok": verification_ok,
            "run_id": state.get("run_id") if state else None,
            "head": state.get("head") if state else None,
            "worker_pid": state.get("worker_pid") if state else None,
            "verify_pid": state.get("verify_pid") if state else None,
            "verify_pgid": state.get("verify_pgid") if state else None,
            "timeout_seconds": state.get("timeout_seconds") if state else None,
            "started_at": state.get("started_at") if state else None,
            "finished_at": state.get("finished_at") if state else None,
            "duration_seconds": state.get("duration_seconds") if state else None,
            "exit_code": state.get("exit_code") if state else None,
            "timed_out": state.get("timed_out") if state else None,
            "failure_kind": state.get("failure_kind") if state else None,
            "working_tree_after": state.get("working_tree_after") if state else None,
            "postconditions": state.get("postconditions") if state else None,
            "log_finalized": state.get("log_finalized") if state else None,
            "log_tail": "",
            "tail_truncated": False,
            "log_bytes_returned": 0,
        }
        if log_result:
            result.update(log_result)
        if recorded != effective:
            result["recorded_status"] = recorded
        if busy is not None:
            result["busy"] = busy
        if state_error or lock_error or required_pass_log_unreadable:
            result["error"] = "Runtime state unavailable."
        if state is None and log_result is None:
            result["error"] = "No readable verification state or log exists yet."
        return result


def _run_verify_worker(
    *,
    config_path: Path,
    project: str,
    lock_fd: int,
    run_id: str,
    expected_head: str,
    started_at: str,
) -> int:
    try:
        config = load_config(config_path)
        profile = config.resolve(project)
        store = StateStore(config.state_dir, profile.project_id)
        _validate_inherited_lock_fd(store, lock_fd)
    except Exception:
        _close_fd(lock_fd)
        return 70

    state, state_error = store.read_state()
    if (
        state_error is not None
        or state is None
        or state.get("project") != project
        or state.get("run_id") != run_id
        or state.get("head") != expected_head
        or state.get("status") != "STARTING"
        or state.get("log_run_id") != run_id
        or state.get("log_finalized") is True
        or state.get("timeout_seconds") != profile.timeout_seconds
    ):
        _close_fd(lock_fd)
        return 70

    before = inspect_repository(profile)
    if not (
        before.get("ok")
        and before.get("head") == expected_head
        and before.get("current_branch") == profile.branch
        and before.get("clean") is True
        and before.get("in_sync") is True
        and before.get("remote_identity_ok") is True
    ):
        try:
            store.append_wrapper_log("\nVERIFY WORKER REFUSED CHANGED REPOSITORY PRECONDITIONS\n")
            terminal = _terminal_state(
                base_state=state,
                profile=profile,
                expected_head=expected_head,
                exit_code=None,
                timed_out=False,
                failure_kind="worker_precondition_failed",
            )
            terminal["status"] = "LAUNCH_FAILED"
            terminal["verification_ok"] = False
            store.commit_terminal_with_log(terminal)
        except (OSError, ValueError):
            pass
        _close_fd(lock_fd)
        return 1

    running = dict(state)
    running.update(
        {
            "status": "RUNNING",
            "worker_pid": os.getpid(),
            "verify_pid": None,
            "verify_pgid": None,
            "started_at": started_at,
            "finished_at": None,
            "duration_seconds": None,
            "exit_code": None,
            "timed_out": False,
            "verification_ok": None,
            "failure_kind": None,
            "working_tree_after": None,
            "log_finalized": False,
        }
    )
    try:
        store.write_state(running)
        log_handle = store.inprogress_log_path.open("ab", buffering=0)
    except (OSError, ValueError):
        _close_fd(lock_fd)
        return 70

    verify_process: subprocess.Popen[Any] | None = None
    owned_verify_pgid: int | None = None
    reader_thread: threading.Thread | None = None
    verify_pipe: Any | None = None
    overflow_event = threading.Event()
    write_error_event = threading.Event()
    try:
        try:
            verify_process = subprocess.Popen(
                list(profile.verify_argv),
                cwd=profile.checkout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_verifier_environment(),
                close_fds=True,
                pass_fds=(lock_fd,),
                start_new_session=True,
                bufsize=0,
            )
            owned_verify_pgid = verify_process.pid
            verify_pipe = verify_process.stdout
            if verify_pipe is None:
                raise OSError("Verifier stdout pipe was not created.")
            reader_thread = threading.Thread(
                target=_drain_verify_output,
                args=(verify_pipe, log_handle, store, overflow_event, write_error_event),
                name="verify-log-reader",
                daemon=True,
            )
            reader_thread.start()
        except (OSError, ValueError, RuntimeError) as exc:
            try:
                if verify_process is not None and owned_verify_pgid is not None:
                    if _terminate_process_group(verify_process, owned_verify_pgid):
                        owned_verify_pgid = None
                if reader_thread is not None and verify_pipe is not None:
                    _join_verify_reader(reader_thread, verify_pipe)
                store.append_log_bytes(
                    log_handle,
                    f"\nVERIFY LAUNCH FAILED: {type(exc).__name__}: {exc}\n".encode(
                        "utf-8", errors="replace"
                    ),
                )
                log_handle.flush()
                os.fsync(log_handle.fileno())
                log_handle.close()
                terminal = _terminal_state(
                    base_state=running,
                    profile=profile,
                    expected_head=expected_head,
                    exit_code=None,
                    timed_out=False,
                    failure_kind="verify_launch_failed",
                )
                terminal["status"] = "LAUNCH_FAILED"
                terminal["verification_ok"] = False
                store.commit_terminal_with_log(terminal)
            except (OSError, ValueError):
                return 70
            return 1

        pgid = verify_process.pid
        running = dict(running)
        running.update({"verify_pid": verify_process.pid, "verify_pgid": pgid})
        try:
            store.write_state(running)
        except (OSError, ValueError):
            return 70

        timed_out = False
        process_group_failure: str | None = None
        exit_code: int | None = None
        deadline = time.monotonic() + profile.timeout_seconds
        while exit_code is None:
            if overflow_event.is_set():
                process_group_failure = "verify_log_limit_exceeded"
                if _terminate_process_group(verify_process, pgid):
                    owned_verify_pgid = None
                exit_code = verify_process.returncode
                if exit_code is None:
                    exit_code = 1
                break
            if write_error_event.is_set():
                if _terminate_process_group(verify_process, pgid):
                    owned_verify_pgid = None
                exit_code = verify_process.returncode
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if overflow_event.is_set():
                    continue
                timed_out = True
                if _terminate_process_group(verify_process, pgid):
                    owned_verify_pgid = None
                exit_code = 124
                break
            try:
                exit_code = verify_process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue

        if process_group_failure != "verify_log_limit_exceeded" and _process_group_exists(pgid):
            if overflow_event.is_set():
                process_group_failure = "verify_log_limit_exceeded"
            else:
                process_group_failure = "verifier_process_group_survived"
            if _terminate_process_group(verify_process, pgid):
                owned_verify_pgid = None
        elif not _process_group_exists(pgid):
            owned_verify_pgid = None

        if reader_thread is None or verify_pipe is None or not _join_verify_reader(reader_thread, verify_pipe):
            return 70
        if overflow_event.is_set():
            timed_out = False
            process_group_failure = "verify_log_limit_exceeded"
            store.append_log_bytes(log_handle, VERIFY_LOG_LIMIT_MARKER)
        elif timed_out:
            store.append_log_bytes(
                log_handle,
                f"\nVERIFY TIMED OUT AFTER {profile.timeout_seconds} SECONDS\n".encode("utf-8"),
            )
        elif process_group_failure == "verifier_process_group_survived":
            store.append_log_bytes(log_handle, b"\nVERIFY PROCESS GROUP SURVIVED LEADER EXIT\n")

        if write_error_event.is_set():
            return 70

        log_handle.flush()
        os.fsync(log_handle.fileno())
        log_handle.close()
        terminal = _terminal_state(
            base_state=running,
            profile=profile,
            expected_head=expected_head,
            exit_code=exit_code,
            timed_out=timed_out,
            failure_kind=process_group_failure,
        )
        store.commit_terminal_with_log(terminal)
        return 0 if terminal.get("status") == "PASS" else 1
    except (OSError, ValueError):
        return 70
    finally:
        if verify_process is not None and owned_verify_pgid is not None:
            if _terminate_process_group(verify_process, owned_verify_pgid):
                owned_verify_pgid = None
        if reader_thread is not None and verify_pipe is not None and reader_thread.is_alive():
            _join_verify_reader(reader_thread, verify_pipe)
        try:
            if not log_handle.closed:
                log_handle.close()
        except Exception:
            pass
        _close_fd(lock_fd)


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--verify-worker", action="store_true")
    parser.add_argument("--config", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--started-at", required=True)
    return parser


def _main() -> int:
    if "--verify-worker" not in sys.argv[1:]:
        return 2
    args = _worker_parser().parse_args(sys.argv[1:])
    return _run_verify_worker(
        config_path=Path(args.config),
        project=args.project,
        lock_fd=args.lock_fd,
        run_id=args.run_id,
        expected_head=args.head,
        started_at=args.started_at,
    )


if __name__ == "__main__":
    raise SystemExit(_main())
