from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_runtime.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def write_config(self, body: str) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "runtime.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def valid_text(self, **overrides: str) -> str:
        values = {
            "state_dir": '"/tmp/agent-runtime-state"',
            "checkout": '"/tmp/disposable-checkout"',
            "timeout_seconds": "3600",
            "verify_argv": '["./verify"]',
            "disposable": "true",
        }
        values.update(overrides)
        return f'''version = 1\nstate_dir = {values["state_dir"]}\n\n[projects.example-main]\nrepository = "owner/repo"\ncheckout = {values["checkout"]}\nremote = "origin"\nexpected_remote_url = "git@github.com:owner/repo.git"\nbranch = "main"\nverify_argv = {values["verify_argv"]}\ntimeout_seconds = {values["timeout_seconds"]}\ndisposable = {values["disposable"]}\n'''

    def test_valid_profile_accepted(self) -> None:
        config = load_config(self.write_config(self.valid_text()))
        profile = config.resolve("example-main")
        self.assertEqual(profile.repository, "owner/repo")
        self.assertEqual(profile.branch, "main")
        self.assertEqual(profile.verify_argv, ("./verify",))
        self.assertTrue(profile.disposable)

    def test_unknown_project_rejected_exactly(self) -> None:
        config = load_config(self.write_config(self.valid_text()))
        with self.assertRaises(ConfigError):
            config.resolve("example-main; rm -rf /")
        with self.assertRaises(ConfigError):
            config.resolve("EXAMPLE-main")

    def test_relative_checkout_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write_config(self.valid_text(checkout='"relative/path"')))

    def test_invalid_timeout_rejected(self) -> None:
        for value in ("0", "3601", "-1", '"60"'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_config(self.write_config(self.valid_text(timeout_seconds=value)))

    def test_verify_command_must_be_nonempty_list_of_strings(self) -> None:
        for value in ('"./verify"', "[]", '["./verify", 7]', '[""]'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_config(self.write_config(self.valid_text(verify_argv=value)))

    def test_disposable_must_be_boolean(self) -> None:
        for value in ('"true"', "1", "[]"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_config(self.write_config(self.valid_text(disposable=value)))

    def test_malformed_and_unknown_fields_fail_closed(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write_config("this is not toml = ["))
        with self.assertRaises(ConfigError):
            load_config(self.write_config(self.valid_text() + "unexpected = true\n"))

    def test_state_dir_and_project_id_are_bounded(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write_config(self.valid_text(state_dir='"relative/state"')))
        text = self.valid_text().replace("example-main", "x" * 65)
        with self.assertRaises(ConfigError):
            load_config(self.write_config(text))


if __name__ == "__main__":
    unittest.main()
