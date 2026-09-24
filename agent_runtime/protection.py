from __future__ import annotations

import json
import os
import re
import select
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

LEGACY_RUNTIME_LAUNCHD_LABEL = "com.picmao.agent-runtime-runtime"
MODERN_RUNTIME_LAUNCHD_LABEL = "com.picmao.agent-runtime-runtime-service"
CURRENT_RUNTIME_LAUNCHD_LABEL = MODERN_RUNTIME_LAUNCHD_LABEL
PROTECTED_RUNTIME_LAUNCHD_LABELS = frozenset({LEGACY_RUNTIME_LAUNCHD_LABEL, MODERN_RUNTIME_LAUNCHD_LABEL})
PROTECTED_PORT = 8080
MAX_AUDIT_EVENTS = 20
PROCESS_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
PROCESS_SNAPSHOT_MAX_ROWS = 8192
PROCESS_SNAPSHOT_DEADLINE_SECONDS = 2.0
_PROCESS_SNAPSHOT_READ_CHUNK_BYTES = 64 * 1024


class ProtectedRuntimeDenied(PermissionError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"PROTECTED_RUNTIME: {category}")


def _default_runtime_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_protected_runtime_path(path: Path, *, runtime_root: Path | None = None) -> bool:
    """Return whether an absolute normalized path is at or below the protected Runtime root."""

    protected_root = (runtime_root or _default_runtime_root()).expanduser().resolve()
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError("protected runtime path checks require an absolute path")
    try:
        candidate.relative_to(protected_root)
    except ValueError:
        return False
    return True


def _default_audit_file() -> Path:
    return Path.home() / "Library" / "Application Support" / "Agent Runtime" / "protected-attempts.json"


class _ProcessSnapshotUnavailable(RuntimeError):
    pass


def _read_process_rows() -> list[tuple[int, int, str]]:
    deadline = time.monotonic() + PROCESS_SNAPSHOT_DEADLINE_SECONDS
    process: subprocess.Popen[bytes] | None = None
    stdout = None
    try:
        process = subprocess.Popen(
            ["/bin/ps", "-ax", "-o", "pid=,ppid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = process.stdout
        if stdout is None:
            raise _ProcessSnapshotUnavailable("process snapshot pipe unavailable")

        payload = bytearray()
        completed_rows = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProcessSnapshotUnavailable("process snapshot deadline exceeded")
            readable, _, _ = select.select([stdout], [], [], remaining)
            if not readable:
                raise _ProcessSnapshotUnavailable("process snapshot deadline exceeded")

            read_size = min(
                _PROCESS_SNAPSHOT_READ_CHUNK_BYTES,
                PROCESS_SNAPSHOT_MAX_BYTES - len(payload) + 1,
            )
            chunk = os.read(stdout.fileno(), read_size)
            if not chunk:
                break
            if len(payload) + len(chunk) > PROCESS_SNAPSHOT_MAX_BYTES:
                raise _ProcessSnapshotUnavailable("process snapshot byte bound exceeded")
            payload.extend(chunk)
            completed_rows += chunk.count(b"\n")
            materialized_rows = completed_rows + int(bool(payload) and not payload.endswith(b"\n"))
            if materialized_rows > PROCESS_SNAPSHOT_MAX_ROWS:
                raise _ProcessSnapshotUnavailable("process snapshot row bound exceeded")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProcessSnapshotUnavailable("process snapshot deadline exceeded")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _ProcessSnapshotUnavailable("process snapshot deadline exceeded") from exc
        if returncode != 0:
            raise _ProcessSnapshotUnavailable("process snapshot command failed")

        raw_rows = bytes(payload).splitlines()
        if len(raw_rows) > PROCESS_SNAPSHOT_MAX_ROWS:
            raise _ProcessSnapshotUnavailable("process snapshot row bound exceeded")

        rows: list[tuple[int, int, str]] = []
        for raw in raw_rows:
            parts = raw.strip().split(None, 2)
            if len(parts) != 3:
                raise _ProcessSnapshotUnavailable("process snapshot row malformed")
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError as exc:
                raise _ProcessSnapshotUnavailable("process snapshot row malformed") from exc
            rows.append((pid, ppid, parts[2].decode("utf-8", errors="replace")))
        return rows
    except _ProcessSnapshotUnavailable:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise _ProcessSnapshotUnavailable("process snapshot unavailable") from exc
    finally:
        if stdout is not None:
            stdout.close()
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait()
            except OSError:
                pass


class ProtectedRuntimeGuard:
    def __init__(
        self,
        *,
        runtime_root: Path | None = None,
        launchd_label: str = CURRENT_RUNTIME_LAUNCHD_LABEL,
        protected_launchd_labels: frozenset[str] = PROTECTED_RUNTIME_LAUNCHD_LABELS,
        audit_file: Path | None = None,
        process_rows_provider: Callable[[], list[tuple[int, int, str]]] = _read_process_rows,
    ) -> None:
        self.runtime_root = (runtime_root or _default_runtime_root()).expanduser().resolve()
        self.launchd_label = launchd_label
        self.protected_launchd_labels = frozenset(protected_launchd_labels)
        self.audit_file = (audit_file or _default_audit_file()).expanduser()
        self._process_rows_provider = process_rows_provider

    def check(self, argv: list[str], *, tool_name: str) -> None:
        self._deny_if_protected(self._classify(argv), tool_name)

    def _deny_if_protected(self, category: str | None, tool_name: str) -> None:
        if category is None:
            return
        self._record(category, tool_name)
        raise ProtectedRuntimeDenied(category)

    def _classify(self, argv: list[str]) -> str | None:
        if not argv:
            return None
        executable = Path(argv[0]).name

        if executable in {"sh", "bash", "zsh"} and len(argv) >= 3 and argv[1] in {"-c", "-lc"}:
            return self._classify_shell_text(argv[2])

        unwrapped = self._unwrap_command(argv)
        if unwrapped != argv:
            return self._classify(unwrapped)

        if self._is_canonical_runtime_launch(argv):
            return "canonical_runtime_launch"

        if executable == "kill":
            targets: list[int] = []
            for token in argv[1:]:
                if token.startswith("-") and not token[1:].isdigit():
                    continue
                try:
                    targets.append(int(token))
                except ValueError:
                    continue
            if targets:
                snapshot = self._canonical_processes_for_classification()
                if snapshot is None:
                    return "process_inspection_unavailable"
                canonical_pids, canonical_pgids, _commands = snapshot
                for target in targets:
                    if target > 0 and target in canonical_pids:
                        return "canonical_process_signal"
                    if target < 0 and abs(target) in canonical_pgids:
                        return "canonical_process_signal"

        if executable == "pkill":
            patterns = [token for token in argv[1:] if not token.startswith("-")]
            if patterns:
                snapshot = self._canonical_processes_for_classification()
                if snapshot is None:
                    return "process_inspection_unavailable"
                _canonical_pids, _canonical_pgids, commands = snapshot
                pattern = patterns[-1]
                for command in commands:
                    try:
                        if re.search(pattern, command):
                            return "canonical_process_match"
                    except re.error:
                        if pattern in command:
                            return "canonical_process_match"

        if executable == "killall":
            names = [token for token in argv[1:] if not token.startswith("-")]
            if names:
                snapshot = self._canonical_processes_for_classification()
                if snapshot is None:
                    return "process_inspection_unavailable"
                _canonical_pids, _canonical_pgids, commands = snapshot
                canonical_names = {Path(command.split()[0]).name for command in commands if command.split()}
                if any(name in canonical_names for name in names):
                    return "canonical_process_match"

        if executable == "launchctl" and any(
            token == label or token.endswith("/" + label)
            for token in argv[1:]
            for label in self.protected_launchd_labels
        ):
            verb = next((token for token in argv[1:] if not token.startswith("-")), "")
            if verb in {"bootout", "stop", "kill", "kickstart", "bootstrap", "start", "enable", "disable"}:
                return "canonical_service_lifecycle"

        if executable == "start.sh" or argv[0].endswith("/start.sh"):
            action = argv[1] if len(argv) > 1 else "start"
            if action in {"start", "stop", "restart", "--serve"}:
                return "canonical_service_lifecycle"

        joined = " ".join(argv)
        if self._contains_protected_port_lifecycle(joined):
            return "protected_port_lifecycle"
        if self._contains_protected_port_rebind(joined):
            return "protected_port_rebind"
        return None

    def _classify_shell_text(self, text: str) -> str | None:
        if self._contains_protected_port_lifecycle(text):
            return "protected_port_lifecycle"
        if self._contains_protected_port_rebind(text):
            return "protected_port_rebind"
        try:
            lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            return None

        segment: list[str] = []
        for token in tokens + [";"]:
            if token and all(char in ";&|()" for char in token):
                if segment:
                    category = self._classify(segment)
                    if category is not None:
                        return category
                    segment = []
                continue
            segment.append(token)
        return None

    @staticmethod
    def _unwrap_command(argv: list[str]) -> list[str]:
        executable = Path(argv[0]).name
        if executable == "env":
            index = 1
            while index < len(argv):
                token = argv[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-u", "--unset", "-C", "--chdir"}:
                    index += 2
                    continue
                if token in {"-S", "--split-string"}:
                    if index + 1 >= len(argv):
                        return argv
                    try:
                        injected = shlex.split(argv[index + 1], posix=True)
                    except ValueError:
                        return argv
                    return injected + argv[index + 2 :] if injected else argv
                if token.startswith(("--unset=", "--chdir=")):
                    index += 1
                    continue
                if token.startswith("--split-string="):
                    try:
                        injected = shlex.split(token.split("=", 1)[1], posix=True)
                    except ValueError:
                        return argv
                    return injected + argv[index + 1 :] if injected else argv
                if token in {"-i", "--ignore-environment", "-0", "--null"}:
                    index += 1
                    continue
                if token.startswith("-"):
                    return argv
                if "=" in token and token.split("=", 1)[0]:
                    index += 1
                    continue
                break
            return argv[index:] if index < len(argv) else argv
        if executable == "nice":
            index = 1
            while index < len(argv):
                token = argv[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-n", "--adjustment"}:
                    index += 2
                    continue
                if token.startswith("--adjustment="):
                    index += 1
                    continue
                if token.startswith("-") and token[1:].isdigit():
                    index += 1
                    continue
                break
            return argv[index:] if index < len(argv) else argv
        if executable in {"command", "nohup", "exec"}:
            index = 1
            while index < len(argv) and argv[index].startswith("-"):
                index += 1
            return argv[index:] if index < len(argv) else argv
        return argv

    def _is_canonical_runtime_launch(self, argv: list[str]) -> bool:
        executable = Path(argv[0]).name
        if executable == "tunnel-client" and self._matches_canonical_tunnel_argv(argv):
            return True
        if executable.startswith("python") and len(argv) >= 3:
            return argv[1:3] == ["-m", "agent_runtime.server"]
        return False

    def _matches_canonical_tunnel_argv(self, argv: list[str]) -> bool:
        if not argv or Path(argv[0]).name != "tunnel-client":
            return False
        command = f"command={self.runtime_root}/.venv/bin/python -m agent_runtime.server,channel=main"
        common = ["run", "--control-plane.poll-channel", "main", "--mcp.command"]
        suffix = ["--health.listen-addr", "127.0.0.1:8080"]
        return argv[1:] in (
            common + [command] + suffix,
            common + [f"command={self.runtime_root}/.venv/bin/python", "-m", "agent_runtime.server,channel=main"] + suffix,
        )

    def _canonical_processes_for_classification(
        self,
    ) -> tuple[set[int], set[int], list[str]] | None:
        try:
            return self._canonical_processes()
        except _ProcessSnapshotUnavailable:
            return None

    def _canonical_processes(self) -> tuple[set[int], set[int], list[str]]:
        try:
            rows = self._process_rows_provider()
        except Exception as exc:
            raise _ProcessSnapshotUnavailable("process snapshot provider failed") from exc
        roots: set[int] = set()
        for pid, _ppid, command in rows:
            try:
                argv = shlex.split(command)
            except ValueError:
                continue
            if self._matches_canonical_tunnel_argv(argv):
                roots.add(pid)
        canonical = set(roots)
        changed = True
        while changed:
            changed = False
            for pid, ppid, _command in rows:
                if ppid in canonical and pid not in canonical:
                    canonical.add(pid)
                    changed = True
        commands = [command for pid, _ppid, command in rows if pid in canonical]
        return canonical, set(roots), commands

    @staticmethod
    def _contains_protected_port_lifecycle(text: str) -> bool:
        lowered = text.lower()
        if "8080" not in lowered:
            return False
        destructive = ("kill", "pkill", "killall", "fuser", "bootout", "launchctl kill")
        return any(token in lowered for token in destructive)

    @staticmethod
    def _contains_protected_port_rebind(text: str) -> bool:
        lowered = text.lower()
        if "8080" not in lowered:
            return False
        binders = (
            "http.server",
            "tcp-listen:8080",
            "--port 8080",
            "--port=8080",
            "listen 8080",
            "-l 8080",
            "-p 8080",
        )
        return any(token in lowered for token in binders)

    def _record(self, category: str, tool_name: str) -> None:
        path = self.audit_file
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError, TypeError):
            current = {}
        events = current.get("events")
        if not isinstance(events, list):
            events = []
        events = [event for event in events if isinstance(event, dict)][-(MAX_AUDIT_EVENTS - 1) :]
        events.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "category": category,
                "tool": tool_name,
            }
        )
        payload = {"version": 1, "blocked_count": len(events), "events": events}
        fd, temp_name = tempfile.mkstemp(prefix=".protected-attempts-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


_PROTECTED_GUARD = ProtectedRuntimeGuard()
