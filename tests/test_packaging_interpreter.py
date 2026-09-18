from __future__ import annotations

import os
import subprocess
import sys
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



class PackagingRuntimeLinkageTests(unittest.TestCase):
    def _fake_package_python(self, package_venv: Path) -> None:
        python = package_venv / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        (package_venv / "lib").mkdir(parents=True, exist_ok=True)

    def _run_materializer(
        self,
        *,
        base_prefix: Path,
        package_venv: Path,
        repo_root: Path,
        linkage: str,
    ) -> subprocess.CompletedProcess[str]:
        script = f"""
source "{HELPER}"
fake_base="$4"
fake_linkage="$5"
packaging_python_base_prefix() {{ printf '%s\\n' "$fake_base"; }}
packaging_python_linkage_dependencies() {{ printf '%s\\n' "$fake_linkage"; }}
materialize_packaging_python_runtime "$1" "$2" "$3" "TEST ERROR"
"""
        return subprocess.run(
            [
                "/bin/bash",
                "-c",
                script,
                "bash",
                sys.executable,
                str(package_venv),
                str(repo_root),
                str(base_prefix),
                linkage,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_linked_layout_materializes_required_libpython_as_owned_equal_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            base = temp / "canonical"
            package_venv = temp / "package-venv"
            repo_root = temp / "repo"
            source = base / "lib" / "libpython3.13.dylib"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"canonical-libpython-bytes")
            self._fake_package_python(package_venv)

            result = self._run_materializer(
                base_prefix=base,
                package_venv=package_venv,
                repo_root=repo_root,
                linkage="@executable_path/../lib/libpython3.13.dylib",
            )

            target = package_venv / "lib" / "libpython3.13.dylib"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file())
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_missing_canonical_relative_library_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            base = temp / "canonical"
            (base / "lib").mkdir(parents=True)
            package_venv = temp / "package-venv"
            self._fake_package_python(package_venv)

            result = self._run_materializer(
                base_prefix=base,
                package_venv=package_venv,
                repo_root=temp / "repo",
                linkage="@executable_path/../lib/libpython3.13.dylib",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("required runtime library", result.stderr)
            self.assertFalse((package_venv / "lib" / "libpython3.13.dylib").exists())

    def test_non_linked_layout_is_valid_noop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            base = temp / "canonical"
            (base / "lib").mkdir(parents=True)
            package_venv = temp / "package-venv"
            self._fake_package_python(package_venv)

            result = self._run_materializer(
                base_prefix=base,
                package_venv=package_venv,
                repo_root=temp / "repo",
                linkage="/usr/lib/libSystem.B.dylib",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list((package_venv / "lib").iterdir()), [])

    def test_unsupported_relative_linkage_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            base = temp / "canonical"
            (base / "lib").mkdir(parents=True)
            package_venv = temp / "package-venv"
            self._fake_package_python(package_venv)

            result = self._run_materializer(
                base_prefix=base,
                package_venv=package_venv,
                repo_root=temp / "repo",
                linkage="@rpath/libpython3.13.dylib",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("unsupported relative linkage", result.stderr)

    def test_unsafe_relative_library_name_fails_closed_and_cannot_escape_lib(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            base = temp / "canonical"
            (base / "lib").mkdir(parents=True)
            package_venv = temp / "package-venv"
            self._fake_package_python(package_venv)

            result = self._run_materializer(
                base_prefix=base,
                package_venv=package_venv,
                repo_root=temp / "repo",
                linkage="@executable_path/../lib/../../escape.dylib",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("unsafe relative runtime library", result.stderr)
            self.assertFalse((temp / "escape.dylib").exists())

    def test_checkout_venv_cannot_supply_runtime_library(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            repo_root = temp / "repo"
            base = repo_root / ".venv" / "base"
            source = base / "lib" / "libpython3.13.dylib"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"must-not-package-checkout")
            package_venv = temp / "package-venv"
            self._fake_package_python(package_venv)

            result = self._run_materializer(
                base_prefix=base,
                package_venv=package_venv,
                repo_root=repo_root,
                linkage="@executable_path/../lib/libpython3.13.dylib",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("checkout .venv", result.stderr)
            self.assertFalse((package_venv / "lib" / "libpython3.13.dylib").exists())

    def test_package_smokes_copied_python_before_exact_hash_locked_pip(self) -> None:
        package = PACKAGE.read_text()
        create = '"$PYTHON_BIN" -m venv --copies --without-pip "$PACKAGE_VENV"'
        materialize = 'materialize_packaging_python_runtime "$PYTHON_BIN" "$PACKAGE_VENV" "$REPO_ROOT" "PACKAGE ERROR"'
        smoke = '"$PACKAGE_VENV/bin/python" -c \'pass\''
        pip = (
            '"$PYTHON_BIN" -m pip --disable-pip-version-check --python "$PACKAGE_VENV/bin/python" \\\n'
            '  install --require-hashes -r "$SOURCE_ROOT/requirements.lock" >/dev/null'
        )

        for fragment in (create, materialize, smoke, pip):
            self.assertIn(fragment, package)
        self.assertLess(package.index(create), package.index(materialize))
        self.assertLess(package.index(materialize), package.index(smoke))
        self.assertLess(package.index(smoke), package.index(pip))
        self.assertNotIn('cp -R -L "$REPO_ROOT/.venv"', package)


if __name__ == "__main__":
    unittest.main()
