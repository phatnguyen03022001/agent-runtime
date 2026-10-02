from __future__ import annotations

import gzip
import hashlib
import io
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import tarfile
import warnings
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "macos" / "package_provenance.py"


def load_module():
    if not MODULE_PATH.is_file():
        raise AssertionError("macos/package_provenance.py must exist")
    spec = importlib.util.spec_from_file_location("package_provenance", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


class PackageProvenanceTests(unittest.TestCase):
    def test_export_head_requires_clean_checkout_and_exports_exact_committed_bytes(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            repo = temp / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test")
            payload = repo / "payload.txt"
            payload.write_text("committed\n")
            git(repo, "add", "payload.txt")
            git(repo, "commit", "-qm", "fixture")
            expected_revision = git(repo, "rev-parse", "HEAD")
            expected_tree = git(repo, "rev-parse", "HEAD^{tree}")

            stage = temp / "stage"
            revision, tree = provenance.export_head(repo, stage)
            self.assertEqual(revision, expected_revision)
            self.assertEqual(tree, expected_tree)
            self.assertEqual((stage / "payload.txt").read_text(), "committed\n")
            self.assertFalse((stage / ".git").exists())

            payload.write_text("dirty tracked\n")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "clean"):
                provenance.export_head(repo, temp / "tracked-dirty")
            git(repo, "checkout", "--", "payload.txt")
            (repo / "untracked.txt").write_text("dirty untracked\n")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "clean"):
                provenance.export_head(repo, temp / "untracked-dirty")

    def test_export_head_uses_explicit_data_filter_without_warning_and_rejects_symlinks(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            repo = temp / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test")
            (repo / "payload.txt").write_text("committed\n")
            git(repo, "add", "payload.txt")
            git(repo, "commit", "-qm", "fixture")
            real_extractall = provenance.tarfile.TarFile.extractall
            observed_filters: list[object] = []

            def recording_extractall(self, path=".", members=None, *, numeric_owner=False, filter=None):
                observed_filters.append(filter)
                return real_extractall(self, path=path, members=members, numeric_owner=numeric_owner, filter=filter)

            with mock.patch.object(provenance.tarfile.TarFile, "extractall", recording_extractall):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    provenance.export_head(repo, temp / "safe-stage")

            self.assertEqual(observed_filters, ["data"])
            self.assertFalse(
                any(isinstance(item.message, DeprecationWarning) and "filter" in str(item.message).lower() for item in caught),
                caught,
            )

            (repo / "link.txt").symlink_to("payload.txt")
            git(repo, "add", "link.txt")
            git(repo, "commit", "-qm", "symlink")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "symlink|regular"):
                provenance.export_head(repo, temp / "symlink-stage")

    def test_export_head_rejects_traversal_hardlinks_and_nonregular_entries_before_extraction(self) -> None:
        provenance = load_module()

        def archive_bytes(kind: str) -> bytes:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                if kind == "traversal":
                    info = tarfile.TarInfo("../escape")
                    payload = b"x"
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                elif kind == "hardlink":
                    info = tarfile.TarInfo("hardlink")
                    info.type = tarfile.LNKTYPE
                    info.linkname = "payload.txt"
                    archive.addfile(info)
                elif kind == "fifo":
                    info = tarfile.TarInfo("fifo")
                    info.type = tarfile.FIFOTYPE
                    archive.addfile(info)
                else:
                    raise AssertionError(kind)
            return buffer.getvalue()

        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            for kind, message in (("traversal", "unsafe path"), ("hardlink", "symlink|regular"), ("fifo", "non-regular")):
                with self.subTest(kind=kind):
                    with mock.patch.object(provenance, "git_identity", return_value=("a" * 40, "b" * 40)):
                        with mock.patch.object(provenance, "_run_git", return_value=archive_bytes(kind)):
                            with self.assertRaisesRegex(provenance.PackageProvenanceError, message):
                                provenance.export_head(temp, temp / f"stage-{kind}")

    def _make_manifest_fixture(self, provenance, root: Path):
        runtime = root / "runtime"
        (runtime / "agent_runtime").mkdir(parents=True)
        (runtime / "start.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (runtime / "agent_runtime" / "server.py").write_bytes(b"print('ok')\n")
        manifest = root / "runtime-manifest.json"
        revision = "a" * 40
        tree = "b" * 40
        lock_sha = "c" * 64
        provenance.write_manifest(runtime, manifest, revision, tree, lock_sha)
        return runtime, manifest, revision, tree, lock_sha

    def test_manifest_round_trip_matches_independent_canonical_digest(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            runtime, manifest, revision, tree, lock_sha = self._make_manifest_fixture(provenance, Path(raw))
            data = json.loads(manifest.read_text())
            self.assertEqual(data["schema"], 2)
            self.assertEqual(data["service_management_contract"], "split-v1")
            self.assertEqual(set(data), provenance.MANIFEST_KEYS)
            self.assertEqual(data["runtime_revision"], revision)
            self.assertEqual(data["git_tree"], tree)
            self.assertEqual(data["requirements_lock_sha256"], lock_sha)
            paths = [entry["path"] for entry in data["files"]]
            self.assertEqual(paths, sorted(paths))
            self.assertEqual(len(paths), len(set(paths)))

            lines = []
            for path in sorted(paths):
                target = runtime / path
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                lines.append(f"{path}\t{target.stat().st_size}\t{digest}\n")
            expected = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
            self.assertEqual(data["payload_sha256"], expected)
            manifest_text = manifest.read_text()
            self.assertNotIn(str(Path(raw)), manifest_text)
            self.assertNotIn("CONTROL_PLANE_API_KEY", manifest_text)
            self.assertNotIn("CONTROL_PLANE_TUNNEL_ID", manifest_text)
            provenance.validate_manifest(runtime, manifest, revision, tree, lock_sha)

    def test_manifest_validation_rejects_each_closed_world_failure(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runtime, manifest, revision, tree, lock_sha = self._make_manifest_fixture(provenance, base / "base")
            original = json.loads(manifest.read_text())

            def fixture(name: str):
                root = base / name
                shutil.copytree(runtime, root / "runtime")
                (root / "runtime-manifest.json").write_text(json.dumps(original, indent=2) + "\n")
                return root / "runtime", root / "runtime-manifest.json"

            cases = []
            rt, mf = fixture("missing")
            (rt / original["files"][0]["path"]).unlink()
            cases.append(("missing", rt, mf, revision, tree, lock_sha))

            rt, mf = fixture("extra")
            (rt / "extra.txt").write_text("extra")
            cases.append(("extra", rt, mf, revision, tree, lock_sha))

            rt, mf = fixture("symlink")
            os.symlink("start.sh", rt / "link")
            cases.append(("symlink", rt, mf, revision, tree, lock_sha))

            rt, mf = fixture("size")
            target = rt / original["files"][0]["path"]
            target.write_bytes(target.read_bytes() + b"x")
            cases.append(("size", rt, mf, revision, tree, lock_sha))

            rt, mf = fixture("hash")
            target = rt / original["files"][0]["path"]
            payload = bytearray(target.read_bytes())
            payload[0] ^= 1
            target.write_bytes(payload)
            cases.append(("hash", rt, mf, revision, tree, lock_sha))

            rt, mf = fixture("malformed_json")
            mf.write_text("{not-json\n")
            cases.append(("malformed_json", rt, mf, revision, tree, lock_sha))

            for name, mutate in (
                ("aggregate", lambda data: data.__setitem__("payload_sha256", "0" * 64)),
                ("duplicate", lambda data: data["files"].append(dict(data["files"][0]))),
                ("unsorted", lambda data: data["files"].reverse()),
                ("malformed_path", lambda data: data["files"][0].__setitem__("path", "../escape")),
                ("invalid_revision", lambda data: data.__setitem__("runtime_revision", "invalid")),
                ("invalid_tree", lambda data: data.__setitem__("git_tree", "invalid")),
                ("invalid_lock", lambda data: data.__setitem__("requirements_lock_sha256", "invalid")),
                ("missing_contract", lambda data: data.pop("service_management_contract", None)),
                ("unknown_contract", lambda data: data.__setitem__("service_management_contract", "split-v2")),
                ("malformed_contract", lambda data: data.__setitem__("service_management_contract", 2)),
                ("unknown_schema", lambda data: data.__setitem__("schema", 3)),
                ("extra_manifest_key", lambda data: data.__setitem__("unexpected", "value")),
            ):
                rt, mf = fixture(name)
                data = json.loads(mf.read_text())
                mutate(data)
                mf.write_text(json.dumps(data, indent=2) + "\n")
                cases.append((name, rt, mf, revision, tree, lock_sha))

            cases.extend(
                [
                    ("revision", *fixture("revision"), "0" * 40, tree, lock_sha),
                    ("tree", *fixture("tree"), revision, "0" * 40, lock_sha),
                    ("lock", *fixture("lock"), revision, tree, "0" * 64),
                ]
            )

            for name, rt, mf, expected_revision, expected_tree, expected_lock in cases:
                with self.subTest(name=name):
                    with self.assertRaises(provenance.PackageProvenanceError):
                        provenance.validate_manifest(rt, mf, expected_revision, expected_tree, expected_lock)

    def test_lock_authorizes_canonical_cp313_macos_arm64_native_artifacts(self) -> None:
        lock_text = (ROOT / "requirements.lock").read_text()
        self.assertIn("CPython 3.13.x / cp313 / macOS arm64", lock_text)
        expected = {
            "cffi==2.1.1": "19ee6127ee34de7d83ce3d371ebc5ed91addbdcc39f9ab15ce4eb35a4e534971",
            "cryptography==50.0.1": "b8f852c65863251b9e3a1b8c150ce21e59b522dbb6a7d4bc80e680d38388e986",
            "pydantic_core==2.46.5": "f332f0e72a5a0400141f830744e141bf9f97917878dbe968669e8a7fefea78ff",
            "rpds-py==2026.6.3": "f4d78253f6996be4901669ad25319f842f740eccf4d58e3c7f3dd39e6dde1d8f",
        }
        lock_lines = {line.split(" --hash=", 1)[0]: line for line in lock_text.splitlines() if " --hash=" in line}
        for requirement, digest in expected.items():
            self.assertIn(requirement, lock_lines)
            self.assertIn(f"--hash=sha256:{digest}", lock_lines[requirement])
        self.assertNotIn("661c298b4821edebead0c91edd2b00374d67ad7c5a1f7a91d4442633b79d6a72", lock_text)

    def test_package_contract_uses_lock_fresh_venv_and_no_checkout_venv_copy(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        installer = (ROOT / "install.sh").read_text()
        lock = ROOT / "requirements.lock"
        self.assertTrue(lock.is_file())
        requirement_lines = [line for line in lock.read_text().splitlines() if line and not line.startswith("#")]
        self.assertTrue(requirement_lines)
        for line in requirement_lines:
            self.assertIn("==", line)
            self.assertIn("--hash=sha256:", line)
        self.assertIn("package_provenance.py", package)
        self.assertIn('chmod -R a-w "$SOURCE_ROOT"', package)
        self.assertIn('chmod -R u+w "$TEMP_ROOT"', package)
        self.assertIn('SWIFT_SCRATCH="$TEMP_ROOT/swift-build"', package)
        self.assertIn('--scratch-path "$SWIFT_SCRATCH"', package)
        self.assertIn('PACKAGE_VENV="$TEMP_ROOT/runtime-venv"', package)
        self.assertIn('--without-pip "$PACKAGE_VENV"', package)
        self.assertIn('cp -R "$PACKAGE_VENV" "$RUNTIME/.venv"', package)
        self.assertIn('cp "$SOURCE_PACKAGE_ROOT/runtime_config.py" "$RUNTIME/macos/runtime_config.py"', package)
        self.assertIn('cp "$SOURCE_PACKAGE_ROOT/package_provenance.py" "$RUNTIME/macos/package_provenance.py"', package)
        self.assertIn('cp "$SOURCE_PACKAGE_ROOT/candidate_cutover.py" "$RUNTIME/macos/candidate_cutover.py"', package)
        self.assertIn("--require-hashes", package)
        self.assertNotIn('cp -R -L "$REPO_ROOT/.venv"', package)
        self.assertIn("package_provenance.py", installer)
        self.assertIn("--require-hashes", installer)
        self.assertIn('chmod 600 "$ENV_FILE"', installer)

    def test_package_contract_finalizes_relocatable_python_before_app_copy(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        helper = (ROOT / "macos" / "packaging_python.sh").read_text()
        release_bundle = (ROOT / "macos" / "release_bundle.py").read_text()

        materialize = 'materialize_packaging_python_runtime "$PYTHON_BIN" "$PACKAGE_VENV" "$REPO_ROOT" "PACKAGE ERROR"'
        pip = (
            '"$PYTHON_BIN" -m pip --disable-pip-version-check --python "$PACKAGE_VENV/bin/python" \\\n'
            '  install --require-hashes -r "$SOURCE_ROOT/requirements.lock" >/dev/null'
        )
        finalize = 'finalize_packaging_python_runtime "$PYTHON_BIN" "$PACKAGE_VENV" "$REPO_ROOT" "PACKAGE ERROR"'
        package_smoke = '"$PACKAGE_VENV/bin/python" -c \'import os, platform, sys, sysconfig;'
        app_copy = '/bin/cp -R "$PACKAGE_VENV" "$RUNTIME/.venv"'
        app_smoke = '"$RUNTIME/.venv/bin/python" -c \'import os, platform, sys, sysconfig;'

        for fragment in (materialize, pip, finalize, package_smoke, app_copy, app_smoke):
            self.assertIn(fragment, package)
        self.assertLess(package.index(materialize), package.index(pip))
        self.assertLess(package.index(pip), package.index(finalize))
        self.assertLess(package.index(finalize), package.index(package_smoke))
        self.assertLess(package.index(package_smoke), package.index(app_copy))
        self.assertLess(package.index(app_copy), package.index(app_smoke))
        self.assertNotIn("TMP_PYVENV=", package)
        self.assertNotIn('grep -E \'^(home|include-system-site-packages|version|executable) = \'', package)

        self.assertIn("/usr/bin/install_name_tool -id", helper)
        self.assertIn("@executable_path/../lib/libpython3.13.dylib", helper)
        self.assertIn("sys.base_prefix", helper)
        self.assertIn('rm -f "$package_venv/pyvenv.cfg"', helper)

        self.assertIn("_reject_embedded_operator_home", release_bundle)
        self.assertIn("release candidate embeds an operator HOME path", release_bundle)

    def _staged_candidate(self, root: Path, label: str) -> tuple[Path, Path]:
        stage = root / label
        app = stage / "Agent Runtime.app"
        app.mkdir(parents=True)
        (app / "payload.txt").write_text(label + "\n")
        handoff = stage / "Agent Runtime.candidate.json"
        handoff.write_text(label + "\n")
        return app, handoff

    def test_candidate_publication_preserves_distinct_outputs_and_rejects_collision(self) -> None:
        provenance = load_module()
        self.assertTrue(hasattr(provenance, "publish_candidate"), "publish_candidate must exist")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidates = root / "candidates"
            app_a, handoff_a = self._staged_candidate(root, "stage-a")
            app_b, handoff_b = self._staged_candidate(root, "stage-b")
            app_collision, handoff_collision = self._staged_candidate(root, "stage-collision")
            candidate_a = {"candidate_sha256": "a" * 64}
            candidate_b = {"candidate_sha256": "b" * 64}
            with mock.patch.object(
                provenance,
                "validate_candidate",
                side_effect=[candidate_a, candidate_b, candidate_a],
            ):
                published_a = provenance.publish_candidate(app_a, handoff_a, candidates)
                a_app_bytes = (Path(published_a["candidate_app"]) / "payload.txt").read_bytes()
                a_handoff_bytes = Path(published_a["candidate_handoff"]).read_bytes()
                published_b = provenance.publish_candidate(app_b, handoff_b, candidates)
                self.assertNotEqual(published_a["candidate_app"], published_b["candidate_app"] )
                self.assertEqual((Path(published_a["candidate_app"]) / "payload.txt").read_bytes(), a_app_bytes)
                self.assertEqual(Path(published_a["candidate_handoff"]).read_bytes(), a_handoff_bytes)
                with self.assertRaisesRegex(provenance.PackageProvenanceError, "already exists"):
                    provenance.publish_candidate(app_collision, handoff_collision, candidates)
            self.assertEqual((Path(published_a["candidate_app"]) / "payload.txt").read_bytes(), a_app_bytes)
            self.assertEqual(Path(published_a["candidate_handoff"]).read_bytes(), a_handoff_bytes)
            self.assertTrue(app_collision.parent.is_dir())

    def test_candidate_publication_race_is_atomic_no_replace(self) -> None:
        provenance = load_module()
        self.assertTrue(
            hasattr(provenance, "_rename_candidate_no_replace"),
            "atomic no-replace publication primitive must exist",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidates = root / "candidates"
            app, handoff = self._staged_candidate(root, "stage-race")
            candidate_sha = "c" * 64
            candidate = {"candidate_sha256": candidate_sha}
            final_root = candidates / candidate_sha
            original_rename = provenance._rename_candidate_no_replace
            raced_state: dict[str, object] = {}

            def inject_destination_then_publish(source: Path, destination: Path) -> None:
                self.assertEqual(destination, final_root)
                destination.mkdir(parents=True)
                destination.chmod(0o711)
                os.utime(destination, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
                stat = destination.stat()
                raced_state.update(
                    inode=stat.st_ino,
                    mode=stat.st_mode & 0o777,
                    mtime_ns=stat.st_mtime_ns,
                )
                original_rename(source, destination)

            with mock.patch.object(provenance, "validate_candidate", return_value=candidate):
                with mock.patch.object(
                    provenance,
                    "_rename_candidate_no_replace",
                    side_effect=inject_destination_then_publish,
                ):
                    with self.assertRaisesRegex(provenance.PackageProvenanceError, "already exists"):
                        provenance.publish_candidate(app, handoff, candidates)

            self.assertTrue(final_root.is_dir())
            final_stat = final_root.stat()
            self.assertEqual(final_stat.st_ino, raced_state["inode"])
            self.assertEqual(final_stat.st_mode & 0o777, raced_state["mode"])
            self.assertEqual(final_stat.st_mtime_ns, raced_state["mtime_ns"])
            self.assertEqual(list(final_root.iterdir()), [])
            self.assertTrue(app.is_dir())
            self.assertTrue(handoff.is_file())

    def test_candidate_publication_validation_failure_leaves_no_final_candidate(self) -> None:
        provenance = load_module()
        self.assertTrue(hasattr(provenance, "publish_candidate"), "publish_candidate must exist")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidates = root / "candidates"
            app, handoff = self._staged_candidate(root, "stage-fail")
            with mock.patch.object(
                provenance,
                "validate_candidate",
                side_effect=provenance.PackageProvenanceError("synthetic validation failure"),
            ):
                with self.assertRaisesRegex(provenance.PackageProvenanceError, "synthetic validation failure"):
                    provenance.publish_candidate(app, handoff, candidates)
            self.assertTrue(app.parent.is_dir())
            self.assertFalse(candidates.exists())

    def test_package_contract_stages_then_publishes_without_singleton_deletion(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        self.assertIn('BUILD_ROOT="$REPO_ROOT/build"', package)
        self.assertIn('CANDIDATES_ROOT="$BUILD_ROOT/candidates"', package)
        self.assertIn('STAGED_PUBLICATION="$TEMP_ROOT/candidate"', package)
        self.assertIn('package_provenance.py" publish-zero-cost', package)
        self.assertNotIn('rm -rf "$APP"', package)
        self.assertNotIn('rm -f "$CANDIDATE_HANDOFF"', package)
        self.assertNotIn('APP="$REPO_ROOT/build/Agent Runtime.app"', package)
        self.assertNotIn('CANDIDATE_HANDOFF="$REPO_ROOT/build/Agent Runtime.candidate.json"', package)

    def test_zero_cost_packaging_has_no_launchservices_side_effect_and_preserves_identity_output_contract(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        self.assertNotIn("lsregister", package)
        self.assertNotIn("LaunchServices", package)

        publish = package.index('package_provenance.py" publish-zero-cost')
        validation = package.index(
            '[[ -n "$FINAL_APP" && -n "$FINAL_HANDOFF" && -n "$FINAL_PAYLOAD" && "$CANDIDATE_SHA256" =~ ^[0-9a-f]{64}$ ]]',
            publish,
        )
        output_contract = (
            "printf 'candidate_app=%s\\n' \"$FINAL_APP\"\n"
            "printf 'candidate_handoff=%s\\n' \"$FINAL_HANDOFF\"\n"
            "printf 'initial_payload_release=%s\\n' \"$FINAL_PAYLOAD\"\n"
            "printf 'initial_payload_closure=%s\\n' \"$INITIAL_PAYLOAD_CLOSURE\"\n"
            "printf 'candidate_sha256=%s\\n' \"$CANDIDATE_SHA256\""
        )
        output = package.index(output_contract, validation)
        self.assertGreater(output, validation)
        self.assertEqual(package.count(output_contract), 1)

    def test_zero_cost_packaging_is_explicit_external_payload_and_has_no_paid_authority_lane(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        self.assertIn("--zero-cost", package)
        self.assertNotIn("AGENT_RUNTIME_CODESIGN_IDENTITY", package)
        self.assertNotIn("Developer ID Application", package)
        self.assertNotIn("notarytool", package)
        self.assertNotIn("stapler", package)
        self.assertNotIn("distribution_verify.py", package)
        self.assertNotIn('"$RUNTIME/agent_runtime/"', package)
        self.assertNotIn('mkdir -p "$RUNTIME/agent_runtime"', package)
        self.assertIn("payloads", package)
        self.assertIn("publish-payload", package)
        self.assertIn("substrate-manifest", package)
        self.assertIn("seal-zero-cost", package)
        self.assertIn("publish-zero-cost", package)
        self.assertIn('--sign -', package)
        for identifier in (
            "com.picmao.agent-runtime",
            "com.picmao.agent-runtime.runtime-service",
            "com.picmao.agent-runtime.screen-capture",
            "com.picmao.agent-runtime.python",
        ):
            self.assertIn(identifier, package)

    def test_zero_cost_packaging_signs_every_native_code_with_explicit_identifier_requirement(self) -> None:
        package = (ROOT / "macos" / "package_app.sh").read_text()
        self.assertIn('/usr/bin/codesign --force --sign - --identifier "$identifier"', package)
        self.assertIn('-r="designated => identifier \\"$identifier\\"" "$@"', package)
        self.assertEqual(package.count('/usr/bin/codesign --force --sign -'), 2)
        self.assertIn('CODE_IDENTIFIER="com.picmao.agent-runtime.python"', package)
        self.assertIn('CODE_IDENTIFIER="com.picmao.agent-runtime.native.${NATIVE_DIGEST:0:24}"', package)
        for invocation in (
            'sign_adhoc "$CODE_IDENTIFIER" "$NATIVE_CODE"',
            'sign_adhoc com.picmao.agent-runtime "$MACOS/AgentRuntimeMenuBar"',
            'sign_adhoc com.picmao.agent-runtime.runtime-service "$MACOS/AgentRuntimeRuntimeService"',
            'sign_adhoc com.picmao.agent-runtime.screen-capture "$MACOS/AgentRuntimeScreenCapture"',
            '/usr/bin/codesign --force --sign - --identifier com.picmao.agent-runtime "$APP" \\\n  -r=\'designated => identifier "com.picmao.agent-runtime"\'',
        ):
            self.assertEqual(package.count(invocation), 1, invocation)

    def test_zero_cost_packaging_rejects_non_explicit_modes_before_build(self) -> None:
        package = ROOT / "macos" / "package_app.sh"
        env = dict(os.environ)
        env["AGENT_RUNTIME_PACKAGING_PYTHON"] = str(ROOT / ".venv" / "bin" / "python")
        for argv in ([], ["--distribution"], ["--signing-identity", "fixture"]):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [str(package), *argv],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: package_app.sh --zero-cost", result.stderr)

    def test_historical_paid_distribution_verifier_is_not_reachable_from_current_packaging(self) -> None:
        verifier_path = ROOT / "macos" / "distribution_verify.py"
        self.assertTrue(verifier_path.is_file(), "historical distribution verifier must remain preserved")
        package = (ROOT / "macos" / "package_app.sh").read_text()
        self.assertNotIn("distribution_verify.py", package)


    def test_zero_cost_distribution_seal_validate_publish_is_atomic_and_payload_bound(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stage = root / "stage"
            app = stage / "Agent Runtime.app"
            runtime = app / "Contents/Resources/runtime"
            (runtime / ".venv/bin").mkdir(parents=True)
            (runtime / "macos").mkdir(parents=True)
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": provenance.OWNER,
                "CFBundleExecutable": "AgentRuntimeMenuBar",
            }))
            (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n")
            (runtime / "start.sh").chmod(0o755)
            (runtime / "macos/package_provenance.py").write_text("# bootstrap\n")
            for relative in provenance.ZERO_COST_CODE_IDENTIFIERS:
                target = app / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("#!/bin/sh\nexit 0\n")
                target.chmod(0o755)
            surface_sha = "d" * 64
            provenance.write_substrate_manifest(
                runtime,
                app / "Contents/Resources/runtime-manifest.json",
                "a" * 40,
                "b" * 40,
                "c" * 64,
                python_major_minor="3.13",
                public_tool_count=20,
                public_surface_sha256=surface_sha,
            )
            source = root / "source-agent-runtime"
            source.mkdir()
            (source / "__init__.py").write_text("__all__ = []\n")
            (source / "server.py").write_text("VALUE = 1\n")
            payload = provenance.publish_payload_release(
                source,
                stage / "payloads",
                revision="a" * 40,
                tree="b" * 40,
                requirements_lock_sha256="c" * 64,
                python_major_minor="3.13",
                public_tool_count=20,
                public_surface_sha256=surface_sha,
            )
            payload_release = Path(payload["release_path"])
            handoff = stage / "Agent Runtime.candidate.json"

            def reader(path: Path) -> dict[str, object]:
                relative = path.relative_to(app).as_posix()
                identifier = provenance.ZERO_COST_CODE_IDENTIFIERS[relative]
                return {
                    "identifier": identifier,
                    "team_identifier": None,
                    "designated_requirement": f'designated => identifier "{identifier}"',
                }

            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                sealed = provenance.seal_zero_cost_candidate(
                    app,
                    handoff,
                    payload_release,
                    identity_reader=reader,
                )
                validated = provenance.validate_zero_cost_candidate(
                    app,
                    handoff,
                    payload_release,
                    identity_reader=reader,
                )
                self.assertEqual(validated, sealed)
                published = provenance.publish_zero_cost_distribution(
                    app,
                    handoff,
                    payload_release,
                    root / "candidates",
                    identity_reader=reader,
                )

            final_root = Path(published["candidate_app"]).parent
            self.assertEqual(final_root.name, sealed["candidate_sha256"])
            self.assertEqual(Path(published["initial_payload_release"]).name, sealed["initial_payload_closure"])
            self.assertTrue(Path(published["candidate_app"]).is_dir())
            self.assertTrue(Path(published["candidate_handoff"]).is_file())
            self.assertTrue(Path(published["initial_payload_release"]).is_dir())
            self.assertFalse(stage.exists())

            tampered = Path(published["initial_payload_release"]) / "agent_runtime/server.py"
            tampered.chmod(0o644)
            tampered.write_text("VALUE = 2\n")
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaises(provenance.PackageProvenanceError):
                    provenance.validate_zero_cost_candidate(
                        Path(published["candidate_app"]),
                        Path(published["candidate_handoff"]),
                        Path(published["initial_payload_release"]),
                        identity_reader=reader,
                    )


    def test_zero_cost_candidate_handoff_binds_substrate_and_initial_payload_without_team_identifier(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "Agent Runtime.app"
            runtime = app / "Contents/Resources/runtime"
            (runtime / ".venv/bin").mkdir(parents=True)
            (runtime / "macos").mkdir(parents=True)
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": provenance.OWNER,
                "CFBundleExecutable": "AgentRuntimeMenuBar",
            }))
            (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n")
            (runtime / "start.sh").chmod(0o755)
            (runtime / "macos/package_provenance.py").write_text("# bootstrap\n")
            for relative in provenance.ZERO_COST_CODE_IDENTIFIERS:
                target = app / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("#!/bin/sh\nexit 0\n")
                target.chmod(0o755)
            surface_sha = "d" * 64
            manifest = app / "Contents/Resources/runtime-manifest.json"
            provenance.write_substrate_manifest(
                runtime,
                manifest,
                "a" * 40,
                "b" * 40,
                "c" * 64,
                python_major_minor="3.13",
                public_tool_count=20,
                public_surface_sha256=surface_sha,
            )

            def reader(path: Path) -> dict[str, object]:
                relative = path.relative_to(app).as_posix()
                identifier = provenance.ZERO_COST_CODE_IDENTIFIERS[relative]
                return {
                    "identifier": identifier,
                    "team_identifier": None,
                    "designated_requirement": f'designated => identifier "{identifier}"',
                }

            data = provenance.zero_cost_candidate_handoff_data(
                app,
                initial_payload_closure="e" * 64,
                identity_reader=reader,
            )
            self.assertEqual(data["schema"], provenance.ZERO_COST_CANDIDATE_SCHEMA)
            self.assertEqual(data["signing_mode"], "adhoc")
            self.assertIsNone(data["team_identifier"])
            self.assertEqual(data["initial_payload_closure"], "e" * 64)
            self.assertEqual(data["lifecycle_contract"], provenance.LIFECYCLE_CONTRACT)
            self.assertEqual(data["payload_contract"], provenance.PAYLOAD_CONTRACT)
            self.assertEqual(data["expected_public_tool_count"], 20)
            self.assertEqual(data["expected_public_surface_sha256"], surface_sha)
            self.assertEqual(data["substrate_sha256"], data["candidate_sha256"])
            self.assertEqual(data["source_revision"], "a" * 40)
            self.assertEqual(data["source_tree"], "b" * 40)

            service = (
                app
                / "Contents"
                / "Library"
                / "LaunchAgents"
                / "com.picmao.agent-runtime-runtime-service.plist"
            )
            service.parent.mkdir(parents=True)
            service.write_bytes(plistlib.dumps({
                "Label": "com.picmao.agent-runtime-runtime-service",
                "BundleProgram": "Contents/MacOS/AgentRuntimeRuntimeService",
            }))
            with self.assertRaisesRegex(
                provenance.PackageProvenanceError,
                "lifecycle|ServiceManagement|LaunchAgent",
            ):
                provenance.zero_cost_candidate_handoff_data(
                    app,
                    initial_payload_closure="e" * 64,
                    identity_reader=reader,
                )


    def test_substrate_manifest_binds_external_payload_contract_and_rejects_first_party_source(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime"
            (runtime / ".venv/bin").mkdir(parents=True)
            (runtime / "macos").mkdir(parents=True)
            (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n")
            (runtime / "start.sh").chmod(0o755)
            (runtime / ".venv/bin/python").write_text("#!/bin/sh\nexit 0\n")
            (runtime / ".venv/bin/python").chmod(0o755)
            (runtime / "macos/package_provenance.py").write_text("# bootstrap\n")
            manifest = root / "runtime-manifest.json"
            surface_sha = "d" * 64

            data = provenance.write_substrate_manifest(
                runtime,
                manifest,
                "a" * 40,
                "b" * 40,
                "c" * 64,
                python_major_minor="3.13",
                public_tool_count=20,
                public_surface_sha256=surface_sha,
            )
            self.assertEqual(data["schema"], provenance.SUBSTRATE_SCHEMA)
            self.assertEqual(data["lifecycle_contract"], provenance.LIFECYCLE_CONTRACT)
            self.assertEqual(data["payload_contract"], provenance.PAYLOAD_CONTRACT)
            self.assertEqual(data["expected_public_tool_count"], 20)
            self.assertEqual(data["expected_public_surface_sha256"], surface_sha)
            self.assertNotIn("mcp_package", data)
            self.assertFalse(any(entry["path"].startswith("agent_runtime/") for entry in data["files"]))
            self.assertEqual(
                provenance.validate_substrate_manifest(
                    runtime,
                    manifest,
                    "a" * 40,
                    "b" * 40,
                    "c" * 64,
                ),
                data,
            )

            (runtime / "agent_runtime").mkdir()
            (runtime / "agent_runtime/server.py").write_text("VALUE = 1\n")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "first-party|agent_runtime"):
                provenance.write_substrate_manifest(
                    runtime,
                    root / "invalid.json",
                    "a" * 40,
                    "b" * 40,
                    "c" * 64,
                    python_major_minor="3.13",
                    public_tool_count=20,
                    public_surface_sha256=surface_sha,
                )


    def test_zero_cost_responsible_code_requires_adhoc_exact_identifiers_and_requirements(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            app = Path(raw) / "Agent Runtime.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Resources/runtime/.venv/bin").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": provenance.OWNER,
                "CFBundleExecutable": "AgentRuntimeMenuBar",
            }))
            for relative in provenance.ZERO_COST_CODE_IDENTIFIERS:
                target = app / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("#!/bin/sh\n")
                target.chmod(0o755)

            def reader(path: Path) -> dict[str, object]:
                relative = path.relative_to(app).as_posix()
                identifier = provenance.ZERO_COST_CODE_IDENTIFIERS[relative]
                return {
                    "identifier": identifier,
                    "team_identifier": None,
                    "designated_requirement": f'designated => identifier "{identifier}"',
                }

            identity = provenance.zero_cost_responsible_code_identity(app, identity_reader=reader)
            self.assertEqual(identity["signing_mode"], "adhoc")
            self.assertIsNone(identity["team_identifier"])
            self.assertEqual(set(identity["responsible_code"]), set(provenance.ZERO_COST_CODE_IDENTIFIERS))

            def wrong_team(path: Path) -> dict[str, object]:
                value = reader(path)
                value["team_identifier"] = "ABCDE12345"
                return value

            with self.assertRaisesRegex(provenance.PackageProvenanceError, "TeamIdentifier"):
                provenance.zero_cost_responsible_code_identity(app, identity_reader=wrong_team)

            def wrong_identifier(path: Path) -> dict[str, object]:
                value = reader(path)
                value["identifier"] = "com.example.foreign"
                return value

            with self.assertRaisesRegex(provenance.PackageProvenanceError, "identifier"):
                provenance.zero_cost_responsible_code_identity(app, identity_reader=wrong_identifier)


    def test_external_payload_release_is_content_addressed_and_tamper_evident(self) -> None:
        provenance = load_module()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source" / "agent_runtime"
            source.mkdir(parents=True)
            (source / "__init__.py").write_text("__all__ = []\n")
            (source / "server.py").write_text("VALUE = 1\n")
            releases = root / "releases"
            surface_sha = "d" * 64

            published = provenance.publish_payload_release(
                source,
                releases,
                revision="a" * 40,
                tree="b" * 40,
                requirements_lock_sha256="c" * 64,
                python_major_minor="3.13",
                public_tool_count=20,
                public_surface_sha256=surface_sha,
            )

            closure = published["content_closure"]
            release = Path(published["release_path"])
            self.assertRegex(closure, r"^[0-9a-f]{64}$")
            self.assertEqual(release.name, closure)
            self.assertEqual(release.parent, releases)
            manifest = provenance.validate_payload_release(
                release,
                expected_closure=closure,
                expected_requirements_lock_sha256="c" * 64,
                expected_python_major_minor="3.13",
                expected_public_tool_count=20,
                expected_public_surface_sha256=surface_sha,
            )
            self.assertEqual(manifest["content_closure"], closure)
            self.assertEqual(manifest["source_revision"], "a" * 40)
            self.assertEqual(manifest["source_tree"], "b" * 40)
            self.assertEqual(manifest["lifecycle_contract"], provenance.LIFECYCLE_CONTRACT)
            self.assertEqual(manifest["payload_contract"], provenance.PAYLOAD_CONTRACT)
            self.assertEqual(
                [entry["path"] for entry in manifest["files"]],
                ["agent_runtime/__init__.py", "agent_runtime/server.py"],
            )
            self.assertTrue(all(entry["mode"] == "0444" for entry in manifest["files"]))
            self.assertFalse(any(os.access(release / entry["path"], os.X_OK) for entry in manifest["files"]))

            (release / "agent_runtime/server.py").chmod(0o644)
            (release / "agent_runtime/server.py").write_text("VALUE = 2\n")
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "mutated|closure|inventory"):
                provenance.validate_payload_release(
                    release,
                    expected_closure=closure,
                    expected_requirements_lock_sha256="c" * 64,
                    expected_python_major_minor="3.13",
                    expected_public_tool_count=20,
                    expected_public_surface_sha256=surface_sha,
                )

            unsafe_source = root / "unsafe" / "agent_runtime"
            unsafe_source.mkdir(parents=True)
            executable = unsafe_source / "server.py"
            executable.write_text("VALUE = 1\n")
            executable.chmod(0o755)
            with self.assertRaisesRegex(provenance.PackageProvenanceError, "executable"):
                provenance.publish_payload_release(
                    unsafe_source,
                    root / "unsafe-releases",
                    revision="a" * 40,
                    tree="b" * 40,
                    requirements_lock_sha256="c" * 64,
                    python_major_minor="3.13",
                    public_tool_count=20,
                    public_surface_sha256=surface_sha,
                )


class ReleaseBundleContractTests(unittest.TestCase):
    def _load_release_bundle(self):
        module_path = ROOT / "macos" / "release_bundle.py"
        self.assertTrue(module_path.is_file(), "macos/release_bundle.py must exist")
        spec = importlib.util.spec_from_file_location("release_bundle", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _identity_reader(self, provenance):
        def reader(path: Path) -> dict[str, object]:
            value = path.as_posix()
            for relative, identifier in provenance.ZERO_COST_CODE_IDENTIFIERS.items():
                if value.endswith("/" + relative):
                    return {
                        "identifier": identifier,
                        "team_identifier": None,
                        "designated_requirement": f'designated => identifier "{identifier}"',
                    }
            raise AssertionError(f"unexpected identity target: {path}")

        return reader

    def _candidate_fixture(self, temp: Path, *, payload_server: str = "VALUE = 1\n"):
        release = self._load_release_bundle()
        provenance = release.provenance
        stage = temp / "candidate-stage"
        app = stage / "Agent Runtime.app"
        runtime = app / "Contents/Resources/runtime"
        (runtime / ".venv/bin").mkdir(parents=True)
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": provenance.OWNER,
                    "CFBundleExecutable": "AgentRuntimeMenuBar",
                    "CFBundleShortVersionString": "0.5.1",
                }
            )
        )
        (runtime / "start.sh").write_text("#!/bin/sh\nexit 0\n")
        (runtime / "start.sh").chmod(0o755)
        for relative in provenance.ZERO_COST_CODE_IDENTIFIERS:
            target = app / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)

        surface_sha = "d" * 64
        provenance.write_substrate_manifest(
            runtime,
            app / "Contents/Resources/runtime-manifest.json",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            python_major_minor="3.13",
            public_tool_count=20,
            public_surface_sha256=surface_sha,
        )
        source = temp / "source" / "agent_runtime"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("__all__ = []\n")
        (source / "server.py").write_text(payload_server)
        payload = provenance.publish_payload_release(
            source,
            stage / "payloads",
            revision="a" * 40,
            tree="b" * 40,
            requirements_lock_sha256="c" * 64,
            python_major_minor="3.13",
            public_tool_count=20,
            public_surface_sha256=surface_sha,
        )
        reader = self._identity_reader(provenance)
        with mock.patch.object(provenance, "_verify_codesign", return_value=None):
            provenance.seal_zero_cost_candidate(
                app,
                stage / "Agent Runtime.candidate.json",
                Path(payload["release_path"]),
                identity_reader=reader,
            )
            published = provenance.publish_zero_cost_distribution(
                app,
                stage / "Agent Runtime.candidate.json",
                Path(payload["release_path"]),
                temp / "candidates",
                identity_reader=reader,
            )
        return release, provenance, Path(published["candidate_app"]).parent, reader

    def _build_fixture(self, temp: Path):
        release, provenance, candidate_root, reader = self._candidate_fixture(temp)
        output = temp / "release"
        output.mkdir()
        with mock.patch.object(provenance, "_verify_codesign", return_value=None):
            result = release.build_release_bundle(candidate_root, output, identity_reader=reader)
        archive = Path(result["archive"])
        return release, provenance, candidate_root, reader, output, archive

    def _rewrite_archive_with_extra(self, release, source: Path, destination: Path, kind: str) -> None:
        with source.open("rb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
                with tarfile.open(fileobj=compressed, mode="r:") as original:
                    members = original.getmembers()
                    with destination.open("wb") as target_raw:
                        with gzip.GzipFile(
                            filename="", fileobj=target_raw, mode="wb", mtime=0
                        ) as target_gzip:
                            with tarfile.open(
                                fileobj=target_gzip, mode="w", format=tarfile.PAX_FORMAT
                            ) as target:
                                for member in members:
                                    payload = original.extractfile(member) if member.isreg() else None
                                    target.addfile(member, payload)
                                    if payload is not None:
                                        payload.close()
                                if kind == "duplicate":
                                    member = members[0]
                                    payload = original.extractfile(member) if member.isreg() else None
                                    target.addfile(member, payload)
                                    if payload is not None:
                                        payload.close()
                                elif kind == "traversal":
                                    info = tarfile.TarInfo("../escape")
                                    info.uid = info.gid = 0
                                    info.uname = info.gname = ""
                                    info.mtime = 0
                                    info.mode = 0o600
                                    data = b"x"
                                    info.size = len(data)
                                    target.addfile(info, io.BytesIO(data))
                                elif kind == "symlink":
                                    info = tarfile.TarInfo("Agent Runtime.app/unsafe-link")
                                    info.type = tarfile.SYMTYPE
                                    info.linkname = "Contents/Info.plist"
                                    info.uid = info.gid = 0
                                    info.uname = info.gname = ""
                                    info.mtime = 0
                                    info.mode = 0o700
                                    target.addfile(info)
                                elif kind == "fifo":
                                    info = tarfile.TarInfo("Agent Runtime.app/unsafe-fifo")
                                    info.type = tarfile.FIFOTYPE
                                    info.uid = info.gid = 0
                                    info.uname = info.gname = ""
                                    info.mtime = 0
                                    info.mode = 0o600
                                    target.addfile(info)
                                elif kind == "extra-root":
                                    info = tarfile.TarInfo("unexpected.txt")
                                    info.uid = info.gid = 0
                                    info.uname = info.gname = ""
                                    info.mtime = 0
                                    info.mode = 0o600
                                    data = b"x"
                                    info.size = len(data)
                                    target.addfile(info, io.BytesIO(data))
                                else:
                                    raise AssertionError(kind)

    def _refresh_asset_metadata(self, release, directory: Path, archive: Path) -> None:
        manifest_path = directory / release.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive, "r:gz") as bundle:
            member_count = len(bundle.getmembers())
        manifest["archive_sha256"] = digest
        manifest["archive_size_bytes"] = archive.stat().st_size
        manifest["archive_member_count"] = member_count
        manifest_path.write_bytes(release._canonical_manifest_bytes(manifest))
        (directory / release.CHECKSUMS_NAME).write_text(
            f"{digest}  {archive.name}\n", encoding="utf-8"
        )

    def test_release_bundle_module_exposes_build_and_verify_contract(self) -> None:
        module = self._load_release_bundle()
        self.assertEqual(module.MANIFEST_SCHEMA, 1)
        self.assertTrue(callable(module.build_release_bundle))
        self.assertTrue(callable(module.verify_release_bundle))

    def test_release_bundle_double_build_is_byte_deterministic_and_round_trip_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            release, provenance, candidate_root, reader = self._candidate_fixture(temp)
            first = temp / "first"
            second = temp / "second"
            extracted = temp / "extracted"
            first.mkdir()
            second.mkdir()
            extracted.mkdir()
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                one = release.build_release_bundle(candidate_root, first, identity_reader=reader)
                two = release.build_release_bundle(candidate_root, second, identity_reader=reader)
                verified = release.verify_release_bundle(
                    Path(one["archive"]),
                    first / release.MANIFEST_NAME,
                    first / release.CHECKSUMS_NAME,
                    extract_dir=extracted,
                    identity_reader=reader,
                )

            first_names = sorted(item.name for item in first.iterdir())
            second_names = sorted(item.name for item in second.iterdir())
            self.assertEqual(first_names, second_names)
            self.assertEqual(len(first_names), 3)
            for name in first_names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

            manifest = json.loads((first / release.MANIFEST_NAME).read_text())
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(manifest["owner"], provenance.OWNER)
            self.assertEqual(manifest["runtime_version"], "0.5.1")
            self.assertEqual(manifest["source_revision"], "a" * 40)
            self.assertEqual(manifest["source_tree"], "b" * 40)
            self.assertEqual(manifest["requirements_lock_sha256"], "c" * 64)
            self.assertEqual(manifest["substrate_manifest_schema"], provenance.SUBSTRATE_SCHEMA)
            self.assertEqual(manifest["candidate_handoff_schema"], provenance.ZERO_COST_CANDIDATE_SCHEMA)
            self.assertEqual(manifest["payload_schema"], provenance.PAYLOAD_SCHEMA)
            self.assertEqual(verified["candidate_sha256"], manifest["candidate_sha256"])

            archive = Path(one["archive"])
            self.assertEqual(
                archive.name,
                release.archive_filename("0.5.1", manifest["candidate_sha256"]),
            )
            with tarfile.open(archive, "r:gz") as bundle:
                names = [member.name for member in bundle.getmembers()]
            joined = "\n".join(names).lower()
            for forbidden in (
                ".git",
                ".agent",
                ".env",
                "runtime.env",
                "__pycache__",
                ".pyc",
                "credentials",
            ):
                self.assertNotIn(forbidden, joined)
            self.assertNotIn(str(Path.home()).lower(), joined)
            self.assertEqual(
                (first / release.CHECKSUMS_NAME).read_text().count("\n"),
                1,
            )

    def test_release_bundle_rejects_checksum_manifest_root_content_and_mode_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            release, provenance, candidate_root, reader, output, archive = self._build_fixture(temp)

            checksum = output / release.CHECKSUMS_NAME
            original_checksum = checksum.read_text()
            checksum.write_text(("0" if original_checksum[0] != "0" else "1") + original_checksum[1:])
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaisesRegex(release.ReleaseBundleError, "checksum"):
                    release.verify_release_bundle(
                        archive,
                        output / release.MANIFEST_NAME,
                        checksum,
                        identity_reader=reader,
                    )
            checksum.write_text(original_checksum)

            manifest_path = output / release.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text())
            manifest["source_revision"] = "e" * 40
            manifest_path.write_bytes(release._canonical_manifest_bytes(manifest))
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaisesRegex(release.ReleaseBundleError, "source_revision"):
                    release.verify_release_bundle(
                        archive,
                        manifest_path,
                        checksum,
                        identity_reader=reader,
                    )

            (candidate_root / "unexpected.txt").write_text("x")
            empty = temp / "unexpected-output"
            empty.mkdir()
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaisesRegex(release.ReleaseBundleError, "inventory"):
                    release.build_release_bundle(candidate_root, empty, identity_reader=reader)
            (candidate_root / "unexpected.txt").unlink()

            target = candidate_root / "Agent Runtime.app/Contents/MacOS/AgentRuntimeMenuBar"
            original_bytes = target.read_bytes()
            original_mode = stat.S_IMODE(target.stat().st_mode)
            target.write_bytes(original_bytes + b"x")
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaises(provenance.PackageProvenanceError):
                    release.build_release_bundle(candidate_root, empty, identity_reader=reader)
            target.write_bytes(original_bytes)
            target.chmod(original_mode)
            target.chmod(0o700)
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaises(provenance.PackageProvenanceError):
                    release.build_release_bundle(candidate_root, empty, identity_reader=reader)

    def test_release_bundle_rejects_embedded_operator_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            release, provenance, candidate_root, reader = self._candidate_fixture(
                temp,
                payload_server=f'HOME_MARKER = {str(Path.home())!r}\n',
            )
            output = temp / "release-home"
            output.mkdir()
            with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                with self.assertRaisesRegex(release.ReleaseBundleError, "operator HOME"):
                    release.build_release_bundle(candidate_root, output, identity_reader=reader)

    def test_release_bundle_verify_rejects_traversal_duplicate_symlink_special_and_extra_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            release, provenance, _, reader, output, archive = self._build_fixture(temp)
            original_manifest = (output / release.MANIFEST_NAME).read_bytes()
            original_checksums = (output / release.CHECKSUMS_NAME).read_bytes()

            for kind, pattern in (
                ("traversal", "unsafe"),
                ("duplicate", "duplicate"),
                ("symlink", "symlink|special"),
                ("fifo", "symlink|special"),
                ("extra-root", "root inventory"),
            ):
                with self.subTest(kind=kind):
                    case = temp / kind
                    case.mkdir()
                    bad_archive = case / archive.name
                    self._rewrite_archive_with_extra(release, archive, bad_archive, kind)
                    (case / release.MANIFEST_NAME).write_bytes(original_manifest)
                    (case / release.CHECKSUMS_NAME).write_bytes(original_checksums)
                    self._refresh_asset_metadata(release, case, bad_archive)
                    with mock.patch.object(provenance, "_verify_codesign", return_value=None):
                        with self.assertRaisesRegex(release.ReleaseBundleError, pattern):
                            release.verify_release_bundle(
                                bad_archive,
                                case / release.MANIFEST_NAME,
                                case / release.CHECKSUMS_NAME,
                                identity_reader=reader,
                            )


if __name__ == "__main__":
    unittest.main()
