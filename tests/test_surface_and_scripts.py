from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SurfaceAndScriptsTests(unittest.TestCase):
    def _verify_fixture(self, temp: Path, *, with_venv: bool) -> tuple[Path, Path, Path]:
        repo = temp / "fixture-repo"
        repo.mkdir()
        outside = temp / "outside"
        outside.mkdir()
        (repo / "verify").write_text((ROOT / "verify").read_text())
        (repo / "verify").chmod(0o700)
        for name in ("install.sh", "start.sh"):
            (repo / name).write_text("#!/usr/bin/env bash\n")
        (repo / "agent_runtime").mkdir()
        (repo / "agent_runtime" / "module.py").write_text("")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_module.py").write_text("")
        log = temp / "interpreter.log"

        def fake_interpreter(path: Path) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/usr/bin/env bash\nprintf '%s|%s\\n' \"$0\" \"$*\" >> \"$FAKE_LOG\"\n")
            path.chmod(0o700)

        if with_venv:
            fake_interpreter(repo / ".venv" / "bin" / "python")
        fake_interpreter(temp / "bin" / "python3")
        fake_interpreter(temp / "explicit-python")
        return repo, outside, log

    def _run_fixture_verify(
        self, repo: Path, outside: Path, log: Path, temp: Path, *, python: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {"PATH": f"{temp / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(log)}
        if python is not None:
            env["PYTHON"] = python
        return subprocess.run(
            [str(repo / "verify")],
            cwd=outside,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_verify_uses_checkout_venv_by_default_outside_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            repo, outside, log = self._verify_fixture(temp, with_venv=True)

            result = self._run_fixture_verify(repo, outside, log, temp)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    f"{repo / '.venv' / 'bin' / 'python'}|-m unittest discover -s tests -v",
                    f"{repo / '.venv' / 'bin' / 'python'}|-m py_compile agent_runtime/module.py tests/test_module.py",
                ],
            )

    def test_verify_honors_a_nonempty_explicit_python_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            repo, outside, log = self._verify_fixture(temp, with_venv=True)
            explicit = temp / "explicit-python"

            result = self._run_fixture_verify(repo, outside, log, temp, python=str(explicit))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    f"{explicit}|-m unittest discover -s tests -v",
                    f"{explicit}|-m py_compile agent_runtime/module.py tests/test_module.py",
                ],
            )

    def test_verify_falls_back_to_path_python3_when_the_checkout_venv_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw).resolve()
            repo, outside, log = self._verify_fixture(temp, with_venv=False)

            result = self._run_fixture_verify(repo, outside, log, temp)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    f"{temp / 'bin' / 'python3'}|-m unittest discover -s tests -v",
                    f"{temp / 'bin' / 'python3'}|-m py_compile agent_runtime/module.py tests/test_module.py",
                ],
            )

    def test_public_mcp_surface_is_exactly_four_terminal_tools(self) -> None:
        source = (ROOT / "agent_runtime/server.py").read_text()
        tree = ast.parse(source)
        assigned = {}
        functions = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PUBLIC_TOOL_NAMES":
                        assigned[target.id] = ast.literal_eval(node.value)
            elif isinstance(node, ast.FunctionDef):
                functions.add(node.name)

        self.assertEqual(
            assigned.get("PUBLIC_TOOL_NAMES"),
            ("terminal_exec", "terminal_start", "terminal_poll", "terminal_control"),
        )
        for name in assigned["PUBLIC_TOOL_NAMES"]:
            self.assertIn(name, functions)
        for retired in ("get_head", "sync", "run_verify", "get_last_log"):
            self.assertNotIn(f"def {retired}(", source)

    def test_supported_mcp_registration_exposes_exact_four_tools_with_conservative_annotations(self) -> None:
        class FakeAnnotations:
            __annotations__ = {
                "readOnlyHint": bool,
                "destructiveHint": bool,
                "idempotentHint": bool,
                "openWorldHint": bool,
            }

            def __init__(self, **kwargs):
                self.values = kwargs

        class FakeMCPServer:
            def __init__(self, name: str) -> None:
                self.name = name
                self.tools = {}

            def tool(self, annotations=None):
                def decorator(function):
                    self.tools[function.__name__] = (function, annotations)
                    return function

                return decorator

            def run(self) -> None:
                raise AssertionError("verification must not start the MCP server")

        mcp_package = types.ModuleType("mcp")
        mcp_package.__path__ = []
        server_module = types.ModuleType("mcp.server")
        server_module.MCPServer = FakeMCPServer
        types_module = types.ModuleType("mcp.types")
        types_module.ToolAnnotations = FakeAnnotations

        saved = {name: sys.modules.get(name) for name in ("mcp", "mcp.server", "mcp.types", "agent_runtime.server")}
        try:
            sys.modules["mcp"] = mcp_package
            sys.modules["mcp.server"] = server_module
            sys.modules["mcp.types"] = types_module
            sys.modules.pop("agent_runtime.server", None)
            module = importlib.import_module("agent_runtime.server")
            self.assertEqual(
                tuple(module.mcp.tools),
                ("terminal_exec", "terminal_start", "terminal_poll", "terminal_control"),
            )
            expected = {
                "terminal_exec": (False, True, False, True),
                "terminal_start": (False, True, False, True),
                "terminal_poll": (False, False, False, False),
                "terminal_control": (False, True, False, True),
            }
            for name, values in expected.items():
                _, annotations = module.mcp.tools[name]
                self.assertIsNotNone(annotations)
                self.assertEqual(
                    annotations.values,
                    {
                        "readOnlyHint": values[0],
                        "destructiveHint": values[1],
                        "idempotentHint": values[2],
                        "openWorldHint": values[3],
                    },
                )
        finally:
            for name, value in saved.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_server_declares_conservative_annotations_and_no_env_api(self) -> None:
        source = (ROOT / "agent_runtime/server.py").read_text()
        self.assertIn("read_only=False", source)
        self.assertIn("destructive=True", source)
        self.assertIn("open_world=True", source)
        functions = {
            node.name: node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(
            [arg.arg for arg in functions["terminal_exec"].args.args],
            ["argv", "cwd", "timeout_seconds"],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_start"].args.args],
            ["argv", "cwd"],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_poll"].args.args],
            ["session_id", "cursor", "wait_ms"],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_control"].args.args],
            ["session_id", "action", "data", "rows", "cols"],
        )
        for function in functions.values():
            self.assertNotIn("env", [arg.arg for arg in function.args.args])

    def test_start_is_foreground_tunnel_only(self) -> None:
        text = (ROOT / "start.sh").read_text()
        self.assertIn('exec tunnel-client run --profile "$PROFILE"', text)
        for forbidden in ("nohup", "launchctl", "LaunchAgent", "daemon"):
            self.assertNotIn(forbidden, text)
        for line in text.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.endswith("&") and not stripped.endswith(">&2"), stripped)

    def test_installer_is_narrow_and_derives_workspace_root_from_checkout_parent(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn("AGENT_RUNTIME_WORKSPACE_ROOT", text)
        self.assertIn('WORKSPACE_ROOT="$(dirname "$ROOT")"', text)
        self.assertIn(".venv", text)
        self.assertIn(".env", text)
        self.assertIn("tunnel-client init", text)
        self.assertIn('$ROOT/.venv/bin/python -m agent_runtime.server', text)
        for retired in (
            ".config/agent-runtime",
            ".local/state/agent-runtime",
            ".local/share/agent-runtime",
            "runtime.local.toml",
            "disposable",
            "verify_argv",
        ):
            self.assertNotIn(retired, text)
        self.assertNotIn("/Users/tienphat", text)

    def test_verify_is_deterministic_and_does_not_start_tunnel(self) -> None:
        text = (ROOT / "verify").read_text()
        self.assertIn("unittest discover", text)
        self.assertIn("py_compile", text)
        self.assertNotIn("tunnel-client run", text)
        self.assertNotIn("CONTROL_PLANE_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
