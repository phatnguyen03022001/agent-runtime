from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
import plistlib
import shutil
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


if __name__ == "__main__":
    unittest.main()
