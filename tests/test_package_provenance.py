from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
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
        self.assertIn('CANDIDATES_ROOT="$REPO_ROOT/build/candidates"', package)
        self.assertIn('STAGED_PUBLICATION="$TEMP_ROOT/candidate"', package)
        self.assertIn('package_provenance.py" publish', package)
        self.assertNotIn('rm -rf "$APP"', package)
        self.assertNotIn('rm -f "$CANDIDATE_HANDOFF"', package)
        self.assertNotIn('APP="$REPO_ROOT/build/Agent Runtime.app"', package)
        self.assertNotIn('CANDIDATE_HANDOFF="$REPO_ROOT/build/Agent Runtime.candidate.json"', package)



if __name__ == "__main__":
    unittest.main()
