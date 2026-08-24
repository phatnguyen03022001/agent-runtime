from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.state import MAX_LOG_TAIL_BYTES, MAX_STATE_BYTES, StateStore


class StateTests(unittest.TestCase):
    def make_store(self) -> StateStore:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return StateStore(Path(temp.name), "example-main")

    def test_state_writes_are_atomic_and_bounded(self) -> None:
        store = self.make_store()
        state = {"version": 1, "status": "RUNNING", "run_id": "abc"}
        store.write_state(state)
        self.assertFalse(store.state_tmp_path.exists())
        self.assertEqual(json.loads(store.state_path.read_text()), state)
        with self.assertRaises(ValueError):
            store.write_state({"payload": "x" * MAX_STATE_BYTES})

    def test_corrupt_or_oversized_state_fails_closed(self) -> None:
        store = self.make_store()
        store.ensure_directory()
        store.state_path.write_text("{broken", encoding="utf-8")
        state, error = store.read_state()
        self.assertIsNone(state)
        self.assertIsNotNone(error)
        store.state_path.write_bytes(b"x" * (MAX_STATE_BYTES + 1))
        state, error = store.read_state()
        self.assertIsNone(state)
        self.assertIn("exceeds", error or "")

    def test_returned_log_tail_is_bounded(self) -> None:
        store = self.make_store()
        store.ensure_directory()
        store.log_path.write_bytes(b"a" * (MAX_LOG_TAIL_BYTES * 3))
        result = store.read_log_tail(store.log_path)
        self.assertLessEqual(len(result["log_tail"].encode("utf-8")), MAX_LOG_TAIL_BYTES)
        self.assertTrue(result["tail_truncated"])

    def test_prepare_and_finalize_keep_only_latest_paths(self) -> None:
        store = self.make_store()
        store.prepare_log()
        store.append_wrapper_log("hello\n")
        store.finalize_log()
        self.assertEqual(store.log_path.read_text(), "hello\n")
        self.assertFalse(store.inprogress_log_path.exists())


if __name__ == "__main__":
    unittest.main()
