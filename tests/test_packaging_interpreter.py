from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "macos" / "packaging_python.sh"
PACKAGE = ROOT / "macos" / "package_app.sh"


class PackagingInterpreterTests(unittest.TestCase):
    def _fake_python(self, path: Path, identity: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/bin/sh\n"
            "printf '%s|%s\\n' \"$0\" \"$*\" >> \"$FAKE_PYTHON_LOG\"\n"
            f"printf '%s\\n' '{identity}'\n"
        )
        path.chmod(0o755)

    def _resolve(self, bin_dir: Path, log: Path, *, explicit: str | None = None):
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["FAKE_PYTHON_LOG"] = str(log)
        if explicit is not None:
            env["AGENT_RUNTIME_PACKAGING_PYTHON"] = explicit
        else:
            env.pop("AGENT_RUNTIME_PACKAGING_PYTHON", None)
        return subprocess.run(
            ["/bin/bash", "-c", f'source "{HELPER}"; resolve_packaging_python "TEST ERROR"'],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_resolver_ignores_generic_python3_and_selects_versioned_cp313(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            log = temp / "python.log"
            generic = temp / "bin" / "python3"
            canonical = temp / "bin" / "python3.13"
            self._fake_python(generic, "cpython\t3.14.6\tcpython-314\tcpython-314-darwin\tdarwin\tarm64\t/fake/python3")
            self._fake_python(canonical, "cpython\t3.13.13\tcpython-313\tcpython-313-darwin\tdarwin\tarm64\t/fake/python3.13")

            result = self._resolve(temp / "bin", log)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(canonical))
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].startswith(str(canonical) + "|"), calls)
            self.assertNotIn(str(generic), [call.split("|", 1)[0] for call in calls])

    def test_resolver_rejects_wrong_minor_abi_platform_and_arch(self) -> None:
        bad_identities = {
            "minor": "cpython\t3.12.9\tcpython-313\tcpython-313-darwin\tdarwin\tarm64\t/fake/python3.13",
            "abi": "cpython\t3.13.13\tcpython-313t\tcpython-313t-darwin\tdarwin\tarm64\t/fake/python3.13",
            "platform": "cpython\t3.13.13\tcpython-313\tcpython-313-linux\tlinux\tarm64\t/fake/python3.13",
            "arch": "cpython\t3.13.13\tcpython-313\tcpython-313-darwin\tdarwin\tx86_64\t/fake/python3.13",
        }
        for name, identity in bad_identities.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                log = temp / "python.log"
                candidate = temp / "bin" / "python3.13"
                self._fake_python(candidate, identity)

                result = self._resolve(temp / "bin", log)

                self.assertEqual(result.returncode, 2)
                self.assertIn("unsupported packaging interpreter", result.stderr)
                self.assertIn("CPython 3.13.x/cp313 on macOS arm64", result.stderr)

    def test_package_fails_each_invalid_identity_before_dependency_install(self) -> None:
        bad_identities = {
            "minor": "cpython\t3.12.9\tcpython-313\tcpython-313-darwin\tdarwin\tarm64\t/fake/python3.13",
            "abi": "cpython\t3.13.13\tcpython-313t\tcpython-313t-darwin\tdarwin\tarm64\t/fake/python3.13",
            "platform": "cpython\t3.13.13\tcpython-313\tcpython-313-linux\tlinux\tarm64\t/fake/python3.13",
            "arch": "cpython\t3.13.13\tcpython-313\tcpython-313-darwin\tdarwin\tx86_64\t/fake/python3.13",
        }
        for name, identity in bad_identities.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                temp = Path(raw)
                log = temp / "python.log"
                candidate = temp / "bin" / "python3.13"
                self._fake_python(candidate, identity)
                env = os.environ.copy()
                env["PATH"] = f"{temp / 'bin'}:{env['PATH']}"
                env["FAKE_PYTHON_LOG"] = str(log)
                env.pop("AGENT_RUNTIME_PACKAGING_PYTHON", None)

                result = subprocess.run([str(PACKAGE)], cwd=ROOT, env=env, capture_output=True, text=True, check=False)

                self.assertEqual(result.returncode, 2)
                self.assertIn("unsupported packaging interpreter", result.stderr)
                calls = log.read_text().splitlines()
                self.assertFalse(any("-m pip" in call for call in calls), calls)

    def test_explicit_interpreter_must_be_absolute_and_is_still_validated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            log = temp / "python.log"
            candidate = temp / "python3.13"
            self._fake_python(candidate, "cpython\t3.13.13\tcpython-313\tcpython-313-darwin\tdarwin\tarm64\t/fake/python3.13")

            relative = self._resolve(temp, log, explicit="python3.13")
            valid = self._resolve(temp, log, explicit=str(candidate))

            self.assertEqual(relative.returncode, 2)
            self.assertIn("absolute path", relative.stderr)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(valid.stdout.strip(), str(candidate))


    def test_install_prerequisite_venv_uses_and_validates_canonical_interpreter_before_pip(self) -> None:
        installer = (ROOT / "install.sh").read_text()
        resolve = 'PACKAGING_PYTHON="$(resolve_packaging_python "INSTALL ERROR")"'
        create = '"$PACKAGING_PYTHON" -m venv "$ROOT/.venv"'
        validate = 'validate_packaging_python "$ROOT/.venv/bin/python" "INSTALL ERROR"'
        install = '"$ROOT/.venv/bin/python" -m pip install --require-hashes -r "$ROOT/requirements.lock"'

        for fragment in (resolve, create, validate, install):
            self.assertIn(fragment, installer)
        self.assertLess(installer.index(resolve), installer.index(create))
        self.assertLess(installer.index(create), installer.index(validate))
        self.assertLess(installer.index(validate), installer.index(install))



if __name__ == "__main__":
    unittest.main()
