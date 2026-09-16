from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "macos" / "freeze_candidate.py"


def load_freeze_module():
    if not FREEZE_PATH.is_file():
        raise AssertionError("macos/freeze_candidate.py must exist")
    spec = importlib.util.spec_from_file_location("freeze_candidate", FREEZE_PATH)
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


class CandidateFreezeTests(unittest.TestCase):
    def _repo(self, root: Path) -> tuple[Path, str, str, str]:
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test")
        (repo / "requirements.lock").write_text("fixture-lock\n")
        (repo / "payload.txt").write_text("payload\n")
        (repo / ".gitignore").write_text("build/\n")
        macos = repo / "macos"
        macos.mkdir()
        package = macos / "package_app.sh"
        package.write_text(
            "#!/bin/sh\nset -eu\n"
            "repo=$(cd \"$(dirname \"$0\")/..\" && pwd)\n"
            "fixture=${FREEZE_FIXTURE_ID:-candidate-a}\n"
            "sha=${FREEZE_FIXTURE_SHA:-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd}\n"
            "out=\"$repo/build/candidates/$sha\"\n"
            "mkdir -p \"$out/Agent Runtime.app\"\n"
            "printf '%s\\n' \"$fixture\" > \"$out/Agent Runtime.app/payload.txt\"\n"
            "printf '{\"fixture\":\"%s\"}\\n' \"$fixture\" > \"$out/Agent Runtime.candidate.json\"\n"
            "printf 'candidate_app=%s\\n' \"$out/Agent Runtime.app\"\n"
            "printf 'candidate_handoff=%s\\n' \"$out/Agent Runtime.candidate.json\"\n"
            "printf 'candidate_sha256=%s\\n' \"$sha\"\n"
            "if [ \"${FREEZE_FIXTURE_DRIFT:-}\" = 1 ]; then printf 'drift\\n' >> \"$repo/payload.txt\"; fi\n"
        )
        package.chmod(0o755)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fixture")
        revision = git(repo, "rev-parse", "HEAD")
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        lock_sha = hashlib.sha256((repo / "requirements.lock").read_bytes()).hexdigest()
        return repo, revision, tree, lock_sha

    def test_source_identity_accepts_exact_clean_revision_tree_and_lock(self) -> None:
        freeze = load_freeze_module()
        with tempfile.TemporaryDirectory() as raw:
            repo, revision, tree, lock_sha = self._repo(Path(raw))
            actual = freeze.require_source_identity(repo, revision, tree, lock_sha)
            self.assertEqual(
                actual,
                {
                    "source_revision": revision,
                    "source_tree": tree,
                    "requirements_lock_sha256": lock_sha,
                },
            )

    def test_source_identity_fails_closed_for_dirty_checkout_or_expected_identity_drift(self) -> None:
        freeze = load_freeze_module()
        with tempfile.TemporaryDirectory() as raw:
            repo, revision, tree, lock_sha = self._repo(Path(raw))
            with self.assertRaisesRegex(freeze.FreezeCandidateError, "source revision"):
                freeze.require_source_identity(repo, "0" * 40, tree, lock_sha)
            with self.assertRaisesRegex(freeze.FreezeCandidateError, "source tree"):
                freeze.require_source_identity(repo, revision, "0" * 40, lock_sha)
            with self.assertRaisesRegex(freeze.FreezeCandidateError, "requirements.lock"):
                freeze.require_source_identity(repo, revision, tree, "0" * 64)
            (repo / "payload.txt").write_text("dirty\n")
            with self.assertRaisesRegex(freeze.FreezeCandidateError, "clean Git checkout"):
                freeze.require_source_identity(repo, revision, tree, lock_sha)

    def test_candidate_identity_must_match_expected_source_identity(self) -> None:
        freeze = load_freeze_module()
        expected_revision = "a" * 40
        expected_tree = "b" * 40
        expected_lock = "c" * 64
        candidate = {
            "schema": 1,
            "bundle_identifier": "com.picmao.agent-runtime",
            "source_revision": expected_revision,
            "source_tree": expected_tree,
            "requirements_lock_sha256": expected_lock,
            "record_count": 1,
            "candidate_sha256": "d" * 64,
        }
        self.assertEqual(
            freeze.require_candidate_identity(candidate, expected_revision, expected_tree, expected_lock),
            candidate,
        )
        for field, replacement in (
            ("source_revision", "0" * 40),
            ("source_tree", "0" * 40),
            ("requirements_lock_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                drifted = dict(candidate)
                drifted[field] = replacement
                with self.assertRaisesRegex(freeze.FreezeCandidateError, "candidate identity"):
                    freeze.require_candidate_identity(
                        drifted, expected_revision, expected_tree, expected_lock
                    )


    def test_freeze_uses_exact_package_reported_candidate_pair(self) -> None:
        freeze = load_freeze_module()
        with tempfile.TemporaryDirectory() as raw:
            repo, revision, tree, lock_sha = self._repo(Path(raw))
            expected = {
                "schema": 1,
                "bundle_identifier": "com.picmao.agent-runtime",
                "source_revision": revision,
                "source_tree": tree,
                "requirements_lock_sha256": lock_sha,
                "record_count": 7,
                "candidate_sha256": "d" * 64,
            }
            environment = {"FREEZE_FIXTURE_ID": "candidate-a", "FREEZE_FIXTURE_SHA": "d" * 64}
            with mock.patch.dict("os.environ", environment):
                with mock.patch.object(freeze.provenance, "validate_candidate", return_value=expected) as validate:
                    actual = freeze.freeze_candidate(repo, revision, tree, lock_sha)
            resolved = repo.resolve()
            app = resolved / "build" / "candidates" / ("d" * 64) / "Agent Runtime.app"
            handoff = resolved / "build" / "candidates" / ("d" * 64) / "Agent Runtime.candidate.json"
            validate.assert_called_once_with(app, handoff)
            self.assertEqual(actual["candidate_app"], str(app))
            self.assertEqual(actual["candidate_handoff"], str(handoff))
            self.assertEqual(actual["candidate_sha256"], "d" * 64)
            self.assertEqual(actual["source_revision"], revision)

    def test_two_freezes_keep_both_candidate_outputs_byte_preserved(self) -> None:
        freeze = load_freeze_module()
        with tempfile.TemporaryDirectory() as raw:
            repo, revision, tree, lock_sha = self._repo(Path(raw))
            candidates = []
            for marker in ("a", "b"):
                candidate_sha = marker * 64
                candidate = {
                    "schema": 1,
                    "bundle_identifier": "com.picmao.agent-runtime",
                    "source_revision": revision,
                    "source_tree": tree,
                    "requirements_lock_sha256": lock_sha,
                    "record_count": 7,
                    "candidate_sha256": candidate_sha,
                }
                environment = {
                    "FREEZE_FIXTURE_ID": f"candidate-{marker}",
                    "FREEZE_FIXTURE_SHA": candidate_sha,
                }
                with mock.patch.dict("os.environ", environment):
                    with mock.patch.object(
                        freeze.provenance, "validate_candidate", return_value=candidate
                    ):
                        candidates.append(
                            freeze.freeze_candidate(repo, revision, tree, lock_sha)
                        )

            first_app = Path(candidates[0]["candidate_app"])
            first_handoff = Path(candidates[0]["candidate_handoff"])
            second_app = Path(candidates[1]["candidate_app"])
            second_handoff = Path(candidates[1]["candidate_handoff"])
            self.assertNotEqual(first_app.parent, second_app.parent)
            self.assertEqual((first_app / "payload.txt").read_bytes(), b"candidate-a\n")
            self.assertEqual(first_handoff.read_bytes(), b'{"fixture":"candidate-a"}\n')
            self.assertEqual((second_app / "payload.txt").read_bytes(), b"candidate-b\n")
            self.assertEqual(second_handoff.read_bytes(), b'{"fixture":"candidate-b"}\n')

    def test_freeze_rejects_package_reported_sha_mismatch(self) -> None:
        freeze = load_freeze_module()
        with tempfile.TemporaryDirectory() as raw:
            repo, revision, tree, lock_sha = self._repo(Path(raw))
            candidate = {
                "schema": 1,
                "bundle_identifier": "com.picmao.agent-runtime",
                "source_revision": revision,
                "source_tree": tree,
                "requirements_lock_sha256": lock_sha,
                "record_count": 7,
                "candidate_sha256": "e" * 64,
            }
            environment = {"FREEZE_FIXTURE_ID": "candidate-b", "FREEZE_FIXTURE_SHA": "d" * 64}
            with mock.patch.dict("os.environ", environment):
                with mock.patch.object(freeze.provenance, "validate_candidate", return_value=candidate):
                    with self.assertRaisesRegex(freeze.FreezeCandidateError, "reported candidate SHA-256"):
                        freeze.freeze_candidate(repo, revision, tree, lock_sha)

    def test_freeze_fails_if_source_becomes_dirty_during_package_build(self) -> None:
        freeze = load_freeze_module()
        with tempfile.TemporaryDirectory() as raw:
            repo, revision, tree, lock_sha = self._repo(Path(raw))
            with mock.patch.dict("os.environ", {"FREEZE_FIXTURE_DRIFT": "1"}):
                with mock.patch.object(freeze.provenance, "validate_candidate") as validate:
                    with self.assertRaisesRegex(freeze.FreezeCandidateError, "clean Git checkout"):
                        freeze.freeze_candidate(repo, revision, tree, lock_sha)
            validate.assert_not_called()


    def test_cli_requires_explicit_source_revision_tree_and_lock_identity(self) -> None:
        result = subprocess.run(
            [sys.executable, str(FREEZE_PATH)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result)
        self.assertIn("--source-revision", result.stderr)
        self.assertIn("--source-tree", result.stderr)
        self.assertIn("--requirements-lock-sha256", result.stderr)


if __name__ == "__main__":
    unittest.main()
