"""TASK-0200 synthetic consumer qualification; no live state or control plane.

Run with unittest --failfast. Stop at the first unmet product predicate;
later qualification rows must not be represented as passing by implication.
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import test_runtime_config as config_owner

ROOT = Path(__file__).resolve().parents[1]


class ProductQualificationTests(unittest.TestCase):
    def _admission_case(
        self,
        *,
        existing: bool,
        expected_reason: str,
        output: str | None = None,
        client_exit: int = 1,
        malformed_utf8: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="task0200-") as raw:
            root = Path(raw).resolve()
            home = root / "home"
            workspace = root / "workspace"
            tools = root / "tools"
            for directory in (home, workspace, tools):
                directory.mkdir()
            canonical = home / "Library/Application Support/Agent Runtime/runtime.env"
            values = {
                "CONTROL_PLANE_API_KEY": "TASK0200_SUBMITTED_SYNTHETIC_SECRET",
                "CONTROL_PLANE_TUNNEL_ID": config_owner.RuntimeConfigTests.TUNNEL_ID,
                "AGENT_RUNTIME_WORKSPACE_ROOT": str(workspace),
                "AGENT_RUNTIME_GIT_NAME": "TASK0200 Synthetic Operator",
                "AGENT_RUNTIME_GIT_EMAIL": "task0200@example.invalid",
            }
            before = None
            identity = None
            if existing:
                canonical.parent.mkdir(parents=True)
                predecessor = {
                    **values,
                    "CONTROL_PLANE_API_KEY": "TASK0200_PREDECESSOR_SYNTHETIC_SECRET",
                    "AGENT_RUNTIME_GIT_NAME": "TASK0200 Predecessor",
                }
                canonical.write_bytes(config_owner.runtime_config._prebuilt_payload(predecessor))
                canonical.chmod(0o600)
                before = canonical.read_bytes()
                info = canonical.stat()
                identity = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))

            argv_capture = root / "tunnel-argv.txt"
            if output is not None or malformed_utf8:
                owner = config_owner.RuntimeConfigTests()
                client = owner._write_tunnel_client(root, output=output or "", exit_code=client_exit)
                script = client.read_text()
                script = script.replace(
                    "#!/bin/sh\n",
                    "#!/bin/sh\n"
                    + "printf '%s\\n' \"$@\" > " + shlex.quote(str(argv_capture)) + "\n",
                    1,
                )
                if malformed_utf8:
                    # Invalid UTF-8 from the synthetic tunnel response, with no network.
                    script = script[:script.index("for arg")] + "printf '\\377'\nexit 0\n"
                client.write_text(script)

            env = {
                "HOME": str(home),
                "PATH": str(tools),
                "TMPDIR": str(root),
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            command = [
                sys.executable,
                str(ROOT / "macos/runtime_config.py"),
                "--reconfigure-stdin" if existing else "--prebuilt-stdin",
                str(canonical),
            ]
            result = subprocess.run(
                command,
                input=json.dumps(values),
                env=env,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            # Evaluate persistence and secret safety even when the error mapping fails.
            if existing:
                self.assertEqual(canonical.read_bytes(), before)
                info = canonical.stat()
                self.assertEqual((info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)), identity)
            else:
                self.assertFalse(canonical.exists())
            observed = result.stdout + result.stderr + "\n".join(command)
            if argv_capture.exists():
                observed += argv_capture.read_text()
            self.assertNotIn(values["CONTROL_PLANE_API_KEY"], observed)
            self.assertLess(len((result.stdout + result.stderr).encode()), 65536)
            self.assertNotEqual(result.returncode, 0)
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError:
                self.fail(
                    f"Expected {expected_reason} structured admission error; "
                    f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
                )
            self.assertEqual(response["reason_code"], expected_reason)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")

    def test_q01_missing_tunnel_client_preserves_absent_and_existing_canonical(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                self._admission_case(existing=existing, expected_reason="TUNNEL_CLIENT_UNAVAILABLE")

    def test_q02_401_preserves_config_and_keeps_submitted_secret_out_of_argv_and_output(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                self._admission_case(
                    existing=existing,
                    expected_reason="INVALID_CREDENTIAL",
                    output=json.dumps({"error": {"status": 401, "message": "TASK0200_SUBMITTED_SYNTHETIC_SECRET"}}),
                )

    def test_q03_403_and_404_preserve_config(self) -> None:
        for status, reason in ((403, "TUNNEL_ACCESS_DENIED"), (404, "TUNNEL_NOT_FOUND")):
            for existing in (False, True):
                with self.subTest(status=status, existing=existing):
                    self._admission_case(
                        existing=existing,
                        expected_reason=reason,
                        output=json.dumps({"error": {"status": status}}),
                    )

    def test_q04_transient_and_malformed_json_preserve_config(self) -> None:
        for output, code in ((json.dumps({"error": {"status": 503}}), 1), ("not-json", 0)):
            for existing in (False, True):
                with self.subTest(output=output, existing=existing):
                    self._admission_case(
                        existing=existing,
                        expected_reason="CONTROL_PLANE_UNAVAILABLE",
                        output=output,
                        client_exit=code,
                    )

    def test_q04_undecodable_tunnel_response_maps_unavailable_and_preserves_config(self) -> None:
        self._admission_case(
            existing=True,
            expected_reason="CONTROL_PLANE_UNAVAILABLE",
            malformed_utf8=True,
        )
