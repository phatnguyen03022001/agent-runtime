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
                    f"{repo / '.venv' / 'bin' / 'python'}|-m py_compile verify_tests.py agent_runtime/module.py tests/test_module.py",
                    f"{repo / '.venv' / 'bin' / 'python'}|{repo / 'verify_tests.py'}",
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
                    f"{explicit}|-m py_compile verify_tests.py agent_runtime/module.py tests/test_module.py",
                    f"{explicit}|{repo / 'verify_tests.py'}",
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
                    f"{temp / 'bin' / 'python3'}|-m py_compile verify_tests.py agent_runtime/module.py tests/test_module.py",
                    f"{temp / 'bin' / 'python3'}|{repo / 'verify_tests.py'}",
                ],
            )

    def test_public_mcp_surface_adds_capacity_observer_to_four_terminal_tools(self) -> None:
        source = (ROOT / "agent_runtime/server.py").read_text()
        tree = ast.parse(source)
        public_binding = None
        functions = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PUBLIC_TOOL_NAMES":
                        public_binding = node.value
            elif isinstance(node, ast.FunctionDef):
                functions.add(node.name)

        self.assertIsInstance(public_binding, ast.Name)
        self.assertEqual(public_binding.id, "CAPABILITY_NAMES")
        from agent_runtime.capability_registry import CAPABILITY_NAMES
        self.assertEqual(
            CAPABILITY_NAMES,
            ("terminal_exec", "terminal_start", "terminal_poll", "terminal_control", "terminal_resize", "capacity_observer", "fs_read_batch", "fs_list", "fs_search", "fs_patch", "fs_write", "repo_observer", "repo_diff", "repo_stage", "repo_commit", "repo_fast_forward", "repo_publish", "screen_capture", "runtime_capabilities"),
        )
        for name in CAPABILITY_NAMES:
            self.assertIn(name, functions)
        for retired in ("get_head", "sync", "run_verify", "get_last_log"):
            self.assertNotIn(f"def {retired}(", source)

    def test_supported_mcp_registration_exposes_capacity_observer_with_read_only_annotations(self) -> None:
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
            def __init__(self, name: str, **metadata) -> None:
                self.name = name
                self.metadata = metadata
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
        server_module.__path__ = []
        server_module.MCPServer = FakeMCPServer
        mcpserver_module = types.ModuleType("mcp.server.mcpserver")
        mcpserver_module.__path__ = []
        mcpserver_module.MCPServer = FakeMCPServer
        exceptions_module = types.ModuleType("mcp.server.mcpserver.exceptions")
        exceptions_module.ToolError = type("ToolError", (Exception,), {})
        types_module = types.ModuleType("mcp.types")
        types_module.ToolAnnotations = FakeAnnotations
        types_module.CallToolResult = type("CallToolResult", (), {})
        types_module.TextContent = type("TextContent", (), {})

        module_names = (
            "mcp",
            "mcp.server",
            "mcp.server.mcpserver",
            "mcp.server.mcpserver.exceptions",
            "mcp.types",
            "agent_runtime.server",
        )
        saved = {name: sys.modules.get(name) for name in module_names}
        try:
            sys.modules["mcp"] = mcp_package
            sys.modules["mcp.server"] = server_module
            sys.modules["mcp.server.mcpserver"] = mcpserver_module
            sys.modules["mcp.server.mcpserver.exceptions"] = exceptions_module
            sys.modules["mcp.types"] = types_module
            sys.modules.pop("agent_runtime.server", None)
            module = importlib.import_module("agent_runtime.server")
            self.assertEqual(
                tuple(module.mcp.tools),
                ("terminal_exec", "terminal_start", "terminal_poll", "terminal_control", "terminal_resize", "capacity_observer", "fs_read_batch", "fs_list", "fs_search", "fs_patch", "fs_write", "repo_observer", "repo_diff", "repo_stage", "repo_commit", "repo_fast_forward", "repo_publish", "screen_capture", "runtime_capabilities"),
            )
            expected = {
                "terminal_exec": (False, True, True, True),
                "terminal_start": (False, True, False, True),
                "terminal_poll": (False, False, False, False),
                "terminal_control": (False, True, False, True),
                "terminal_resize": (False, False, True, False),
                "capacity_observer": (True, False, True, False),
                "fs_read_batch": (True, False, True, False),
                "fs_list": (True, False, True, False),
                "fs_search": (True, False, True, False),
                "fs_patch": (False, True, False, False),
                "fs_write": (False, True, False, False),
                "repo_observer": (True, False, True, False),
                "repo_diff": (True, False, True, False),
                "repo_stage": (False, True, False, False),
                "repo_commit": (False, True, False, False),
                "repo_fast_forward": (False, True, True, True),
                "repo_publish": (False, True, True, True),
                "screen_capture": (True, False, True, False),
                "runtime_capabilities": (True, False, True, False),
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
        self.assertIn("TERMINAL_EXEC_CONTRACT.annotations.read_only", source)
        self.assertIn("TERMINAL_EXEC_CONTRACT.annotations.destructive", source)
        self.assertIn("TERMINAL_EXEC_CONTRACT.annotations.open_world", source)
        self.assertIn("RUNTIME_CAPABILITIES_CONTRACT.annotations.read_only", source)
        functions = {
            node.name: node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(
            [arg.arg for arg in functions["terminal_exec"].args.args],
            ["argv", "cwd", "start_identity", "timeout_seconds"],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_start"].args.args],
            ["argv", "cwd", "start_identity", "mode"],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_poll"].args.args],
            [
                "session_id",
                "start_identity",
                "cursor",
                "wait_ms",
                "wait_for",
                "output",
                "max_output_bytes",
            ],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_control"].args.args],
            ["session_id", "action", "data"],
        )
        self.assertEqual(
            [arg.arg for arg in functions["terminal_resize"].args.args],
            ["session_id", "rows", "cols"],
        )
        for function in functions.values():
            self.assertNotIn("env", [arg.arg for arg in function.args.args])

    def test_install_exposes_bounded_partial_cutover_recovery_command(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn("--recover-partial-cutover)", text)
        branch = text[text.index("--recover-partial-cutover)"):]
        self.assertIn("run_cutover_helper recover", branch)
        self.assertIn("--launchctl", branch)
        self.assertNotIn("resetbtm", branch)
        self.assertNotIn("sfltool", branch)

    def test_install_exposes_bounded_resume_cutover_command(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn("--resume-cutover)", text)
        branch_start = text.index("--resume-cutover)")
        branch = text[branch_start:]
        self.assertIn("run_cutover_helper resume", branch)
        self.assertIn("--launchctl", branch)
        self.assertNotIn("register-runtime", branch.split(";;", 1)[0])
        self.assertNotIn("sfltool", branch.split(";;", 1)[0])

    def test_approval_detection_never_matches_localized_error_text(self) -> None:
        swift = (ROOT / "macos/Sources/AgentRuntimeMenuBar/ServiceManagementController.swift").read_text()
        python = (ROOT / "macos/candidate_cutover.py").read_text()
        self.assertNotIn('"Operation not permitted"', swift)
        self.assertNotIn('"Operation not permitted"', python)
        self.assertIn("SMAppServiceErrorDomain", swift)

    def test_start_uses_launchd_singleton_and_canonical_env_backed_serve_mode(self) -> None:
        text = (ROOT / "start.sh").read_text()
        self.assertIn("com.picmao.agent-runtime-runtime-service", text)
        self.assertIn("protected-runtime-running", text)
        self.assertIn("lifecycle.lock", text)
        self.assertIn("--serve", text)
        self.assertIn("CONTROL_PLANE_TUNNEL_ID", text)
        self.assertIn("--control-plane.poll-channel", text)
        self.assertIn("--health.listen-addr", text)
        self.assertIn("RUNTIME_PATH=\"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\"", text)
        self.assertIn('"PYTHONDONTWRITEBYTECODE": "1"', text)
        self.assertNotIn("--profile-file", text)
        self.assertNotIn("TUNNEL_CLIENT_PROFILE_FILE", text)
        self.assertNotIn("AGENT_RUNTIME_TUNNEL_PROFILE", text)

    def test_installer_registers_app_owned_runtime_service_without_changing_desired_state(self) -> None:
        import plistlib

        installer = (ROOT / "install.sh").read_text()
        cutover = (ROOT / "macos" / "candidate_cutover.py").read_text()
        service_path = (
            ROOT / "macos" / "AppBundle" / "Library" / "LaunchAgents"
            / "com.picmao.agent-runtime-runtime-service.plist"
        )
        service = plistlib.loads(service_path.read_bytes())
        self.assertIn("--install-prebuilt", installer)
        self.assertEqual(service["Label"], "com.picmao.agent-runtime-runtime-service")
        self.assertEqual(service["BundleProgram"], "Contents/MacOS/AgentRuntimeRuntimeService")
        self.assertEqual(service["KeepAlive"], {"SuccessfulExit": False})
        self.assertIs(service["RunAtLoad"], False)
        self.assertIn("protected-runtime-running", cutover)
        self.assertIn('_service_management(target_app, "register-runtime")', cutover)
        self.assertIn('_service_management(target_app, "unregister-runtime")', cutover)
        self.assertIn('"desired_state_present": desired_state.exists()', cutover)

    def test_installer_is_narrow_and_derives_workspace_root_from_checkout_parent(self) -> None:
        installer = (ROOT / "install.sh").read_text()
        cutover = (ROOT / "macos" / "candidate_cutover.py").read_text()
        combined = installer + cutover
        self.assertIn("AGENT_RUNTIME_WORKSPACE_ROOT", installer)
        self.assertIn('WORKSPACE_ROOT="$(dirname "$ROOT")"', installer)
        self.assertIn(".venv", installer)
        self.assertIn(".env", installer)
        self.assertIn("CONTROL_PLANE_TUNNEL_ID", installer)
        self.assertIn("--control-plane.poll-channel", installer)
        self.assertIn('"EnvironmentVariables"', cutover)
        self.assertNotIn("--tunnel-client", installer)
        self.assertIn("tunnel-client is required; install the official OpenAI tunnel-client first.", installer)
        self.assertIn('runtime_python + " -m agent_runtime.server,channel=main"', installer)
        for retired in (
            ".config/agent-runtime",
            ".local/state/agent-runtime",
            ".local/share/agent-runtime",
            "runtime.local.toml",
            "disposable",
            "verify_argv",
        ):
            self.assertNotIn(retired, combined)
        self.assertNotIn("/Users/tienphat", combined)
        self.assertNotIn("--profile-file", combined)
        self.assertNotIn("tunnel-client init", combined)

    def test_verify_is_deterministic_and_does_not_start_tunnel(self) -> None:
        text = (ROOT / "verify").read_text()
        self.assertIn("verify_tests.py", text)
        self.assertIn("py_compile", text)
        self.assertLess(text.index("py_compile"), text.rindex('"$ROOT/verify_tests.py"'))
        self.assertNotIn("unittest discover", text)
        self.assertNotIn("tunnel-client run", text)
        self.assertNotIn("CONTROL_PLANE_API_KEY", text)


    def test_menu_bar_surfaces_protection_warning_and_docs_state_enforcement_boundary(self) -> None:
        source = (ROOT / "macos" / "Sources" / "AgentRuntimeMenuBar" / "ControlPanelController.swift").read_text()
        self.assertIn("retained", source)
        self.assertNotIn("blocked · last:", source)
        docs = (ROOT / "README.md").read_text()
        self.assertIn("protected singleton", docs)
        self.assertIn("root/sudo", docs)
        self.assertIn("malicious local administrator", docs)
        self.assertIn("exactly nineteen public tools", docs)
        self.assertIn("AGENT_RUNTIME_MAX_PARALLELISM", docs)
        self.assertIn("capacity_observer", docs)

    def test_threat_model_states_filter_boundary_and_same_uid_limitations(self) -> None:
        docs = (ROOT / "README.md").read_text()
        threat_model = (ROOT / "THREAT_MODEL.md").read_text()
        for phrase in (
            "recognized intent",
            "same UID",
            "not a sandbox",
            "Executor governance",
            "separate future isolation architecture",
        ):
            self.assertIn(phrase, threat_model)
        self.assertIn("THREAT_MODEL.md", docs)
        self.assertIn("same UID", docs)
        self.assertIn("not a sandbox", docs)

    def test_native_app_bundle_is_menu_bar_only(self) -> None:
        import plistlib

        info = plistlib.loads((ROOT / "macos" / "AppBundle" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], "com.picmao.agent-runtime")
        self.assertIs(info["LSUIElement"], True)
        self.assertEqual(info["CFBundleExecutable"], "AgentRuntimeMenuBar")

    def test_installer_uses_main_app_service_management_for_login_without_runtime_autostart(self) -> None:
        import plistlib

        installer = (ROOT / "install.sh").read_text()
        cutover = (ROOT / "macos" / "candidate_cutover.py").read_text()
        controller = (
            ROOT / "macos" / "Sources" / "AgentRuntimeMenuBar" / "ServiceManagementController.swift"
        ).read_text()
        service = plistlib.loads((
            ROOT / "macos" / "AppBundle" / "Library" / "LaunchAgents"
            / "com.picmao.agent-runtime-runtime-service.plist"
        ).read_bytes())
        self.assertIn("SMAppServiceControl(service: .mainApp)", controller)
        self.assertIn("SMAppServiceControl(service: .agent(plistName: runtimePlistName))", controller)
        self.assertIs(service["RunAtLoad"], False)
        self.assertIn('_service_management(target_app, "register-main")', cutover)
        self.assertIn('_service_management(target_app, "register-runtime")', cutover)
        self.assertIn("--recover-partial-cutover", installer)
        self.assertIn("Agent Runtime.app", installer)
        self.assertIn("--health.listen-addr", installer)
        self.assertNotIn("tunnel-client run", installer + cutover)

    def test_installed_runtime_is_package_owned_without_checkout_config_pointer(self) -> None:
        installer = (ROOT / "install.sh").read_text()
        cutover = (ROOT / "macos/candidate_cutover.py").read_text()
        package = (ROOT / "macos/package_app.sh").read_text()
        provenance = (ROOT / "macos/package_provenance.py").read_text()
        self.assertIn('Contents/Resources/runtime/start.sh', cutover)
        self.assertIn("Resources/runtime", installer)
        self.assertIn("runtime-manifest.json", provenance)
        self.assertIn("runtime.env", installer)
        self.assertNotIn("env-path.txt", installer + cutover)
        self.assertNotIn('"RUNTIME_ENV_FILE":', cutover)
        self.assertIn('"RUNTIME_ENV_FILE" in environment', cutover)
        self.assertIn('cp "$SOURCE_ROOT/start.sh" "$RUNTIME/start.sh"', package)
        self.assertIn('package_provenance.py" stage "$REPO_ROOT" "$SOURCE_ROOT"', package)
        self.assertIn('--without-pip "$PACKAGE_VENV"', package)
        self.assertIn("--require-hashes", package)
        self.assertIn('cp -R "$PACKAGE_VENV" "$RUNTIME/.venv"', package)
        self.assertNotIn('cp -R -L "$REPO_ROOT/.venv"', package)
        self.assertIn('/usr/bin/strip -S "$MACOS/AgentRuntimeMenuBar"', package)
        self.assertIn("find \"$PACKAGE_VENV\" -type d -name '__pycache__'", package)
        self.assertIn('"$RUNTIME/agent_runtime/"', package)
        self.assertNotIn("checkout-path.txt", package)

    def test_configurable_session_limit_is_documented_and_hard_capped_at_six(self) -> None:
        source = (ROOT / "agent_runtime/session.py").read_text()
        docs = (ROOT / "README.md").read_text()
        self.assertIn("MAX_ACTIVE_SESSIONS = 6", source)
        self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS", source)
        self.assertIn("DEFAULT_SESSION_LIMIT = MAX_ACTIVE_SESSIONS", source)
        self.assertIn("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS", docs)
        self.assertIn("session-limit", docs)



if __name__ == "__main__":
    unittest.main()
