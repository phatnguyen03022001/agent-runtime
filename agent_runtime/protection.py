from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

LAUNCHD_LABEL = "com.picmao.agent-runtime-runtime"
PROTECTED_PORT = 8080
MAX_AUDIT_EVENTS = 20


class ProtectedRuntimeDenied(PermissionError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"PROTECTED_RUNTIME: {category}")


def _default_runtime_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_audit_file() -> Path:
    return Path.home() / "Library" / "Application Support" / "Agent Runtime" / "protected-attempts.json"


def _read_process_rows() -> list[tuple[int, int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-ax", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    rows: list[tuple[int, int, str]] = []
    for raw in result.stdout.splitlines():
        parts = raw.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


class ProtectedRuntimeGuard:
    def __init__(
        self,
        *,
        runtime_root: Path | None = None,
        launchd_label: str = LAUNCHD_LABEL,
        audit_file: Path | None = None,
        process_rows_provider: Callable[[], list[tuple[int, int, str]]] = _read_process_rows,
    ) -> None:
        self.runtime_root = (runtime_root or _default_runtime_root()).expanduser().resolve()
        self.launchd_label = launchd_label
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
        canonical_pids, canonical_pgids, commands = self._canonical_processes()

        if executable in {"sh", "bash", "zsh"} and len(argv) >= 3 and argv[1] in {"-c", "-lc"}:
            return self._classify_shell_text(argv[2])

        unwrapped = self._unwrap_command(argv)
        if unwrapped != argv:
            return self._classify(unwrapped)

        if self._is_canonical_runtime_launch(argv):
            return "canonical_runtime_launch"

        if executable == "kill":
            for token in argv[1:]:
                if token.startswith("-") and not token[1:].isdigit():
                    continue
                try:
                    target = int(token)
                except ValueError:
                    continue
                if target > 0 and target in canonical_pids:
                    return "canonical_process_signal"
                if target < 0 and abs(target) in canonical_pgids:
                    return "canonical_process_signal"

        if executable == "pkill":
            patterns = [token for token in argv[1:] if not token.startswith("-")]
            if patterns:
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
            canonical_names = {Path(command.split()[0]).name for command in commands if command.split()}
            if any(name in canonical_names for name in names):
                return "canonical_process_match"

        if executable == "launchctl" and self.launchd_label in " ".join(argv[1:]):
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

    def _canonical_processes(self) -> tuple[set[int], set[int], list[str]]:
        rows = self._process_rows_provider()
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
        count = int(current.get("blocked_count", 0)) + 1
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
        payload = {"version": 1, "blocked_count": count, "events": events}
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
