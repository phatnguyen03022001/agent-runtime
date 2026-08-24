from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SERVER = ROOT / "agent_runtime" / "server.py"
RUNTIME_SOURCES = [ROOT / "agent_runtime" / name for name in ("config.py", "git_ops.py", "state.py", "runner.py", "server.py")]


class ServerSurfaceTests(unittest.TestCase):
    def test_exact_public_tool_set_and_project_only_signatures(self) -> None:
        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        tools = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "_tool" for dec in node.decorator_list):
                    tools[node.name] = [arg.arg for arg in node.args.args]
        self.assertEqual(set(tools), {"get_head", "sync", "run_verify", "get_last_log"})
        self.assertEqual(tools, {name: ["project"] for name in tools})

    def test_no_shell_eval_exec_or_public_profile_mutation(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8") for path in RUNTIME_SOURCES)
        self.assertNotIn("shell=True", text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("exec(", text)
        self.assertNotIn("profile_mut", text)
        self.assertNotIn("create_branch", text)
        self.assertNotIn("git push", text.lower())

    def test_malicious_project_is_not_interpreted_as_command_or_path(self) -> None:
        source = SERVER.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("Path(project", source)
        self.assertNotIn("os.system", source)

    def test_no_github_actions_or_extra_server_endpoints(self) -> None:
        self.assertFalse((ROOT / ".github" / "workflows").exists())
        source = SERVER.read_text(encoding="utf-8")
        for forbidden in ("shell", "exec", "run_command", "push", "commit", "create_branch", "merge", "restart", "kill"):
            self.assertNotIn(f"def {forbidden}(", source)


if __name__ == "__main__":
    unittest.main()
