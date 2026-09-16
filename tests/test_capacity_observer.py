from __future__ import annotations

import ctypes
import os
import unittest
from unittest import mock

from agent_runtime import capacity


class CapacityObserverTests(unittest.TestCase):
    def _healthy(self) -> capacity.CapacitySignals:
        return capacity.CapacitySignals(
            active_processors=8,
            load1=2.0,
            cpu_busy_fraction=0.25,
            thermal_state="nominal",
            swap_total_bytes=4 * 1024**3,
            swap_used_bytes=3 * 1024**3,
            swapin_delta_pages=0,
            swapout_delta_pages=0,
            vm_free_bytes=512 * 1024**2,
            vm_inactive_bytes=1024 * 1024**2,
            vm_purgeable_bytes=128 * 1024**2,
            vm_compressor_bytes=2 * 1024**3,
            disk_available_bytes=50 * 1024**3,
            sampled_window_ms=50,
        )

    def _observe(self, configured: str | None, signals: capacity.CapacitySignals | None = None):
        env = {} if configured is None else {capacity.MAX_PARALLELISM_ENV: configured}
        with patch_env(env), mock.patch.object(capacity, "_collect_signals", return_value=signals or self._healthy()):
            return capacity.observe_capacity()

    def test_absent_operator_limit_defaults_to_two(self) -> None:
        result = self._observe(None)
        self.assertEqual(result["capacity_parallelism_ceiling"], 2)

    def test_operator_limit_one_forces_serial_even_when_host_is_healthy(self) -> None:
        result = self._observe("1")
        self.assertEqual(result["capacity_parallelism_ceiling"], 1)
        self.assertIn("LIMIT_OPERATOR_MAX", result["reason_codes"])
        self.assertIn("CAPACITY_X6_AVAILABLE", result["reason_codes"])
        self.assertIn("LIMIT_V2_MAX_6", result["reason_codes"])

    def test_operator_limits_two_six_and_ten_follow_v2_ceiling(self) -> None:
        cases = {"2": 2, "6": 6, "10": 6}
        for configured, expected in cases.items():
            with self.subTest(configured=configured):
                result = self._observe(configured)
                self.assertEqual(result["capacity_parallelism_ceiling"], expected)
                self.assertIn("CAPACITY_X6_AVAILABLE", result["reason_codes"])
                self.assertIn("LIMIT_V2_MAX_6", result["reason_codes"])
                if expected < 6:
                    self.assertIn("LIMIT_OPERATOR_MAX", result["reason_codes"])
                else:
                    self.assertNotIn("LIMIT_OPERATOR_MAX", result["reason_codes"])

    def test_invalid_operator_limit_fails_instead_of_clamping(self) -> None:
        for configured in ("", "0", "11", "-1", "2.0", "many"):
            with self.subTest(configured=configured), patch_env({capacity.MAX_PARALLELISM_ENV: configured}):
                with self.assertRaisesRegex(ValueError, "AGENT_RUNTIME_MAX_PARALLELISM"):
                    capacity.observe_capacity()

    def test_pressure_signals_each_serialize(self) -> None:
        cases = {
            "thermal": (self._healthy().__class__(**{**self._healthy().__dict__, "thermal_state": "fair"}), "LIMIT_THERMAL"),
            "cpu": (self._healthy().__class__(**{**self._healthy().__dict__, "cpu_busy_fraction": 0.90}), "LIMIT_CPU_HEADROOM"),
            "load": (self._healthy().__class__(**{**self._healthy().__dict__, "load1": 8.0}), "LIMIT_SUSTAINED_LOAD"),
            "swap": (self._healthy().__class__(**{**self._healthy().__dict__, "swapout_delta_pages": 1}), "LIMIT_SWAP_ACTIVITY"),
            "memory": (self._healthy().__class__(**{**self._healthy().__dict__, "vm_free_bytes": 1, "vm_inactive_bytes": 1, "vm_purgeable_bytes": 1}), "LIMIT_MEMORY_HEADROOM"),
            "disk": (self._healthy().__class__(**{**self._healthy().__dict__, "disk_available_bytes": 1024**3}), "LIMIT_DISK_HEADROOM"),
        }
        for name, (signals, reason) in cases.items():
            with self.subTest(name=name):
                result = self._observe("10", signals)
                self.assertEqual(result["capacity_parallelism_ceiling"], 1)
                self.assertIn(reason, result["reason_codes"])

    def test_disk_available_uses_statfs_f_bavail_times_f_bsize(self) -> None:
        class FakeLibrary:
            def statfs(self, _path, pointer):
                filesystem = ctypes.cast(pointer, ctypes.POINTER(capacity._StatFS)).contents
                filesystem.f_bsize = 4096
                filesystem.f_bavail = 100
                return 0

        with mock.patch.object(capacity, "_libsystem", return_value=FakeLibrary()):
            self.assertEqual(capacity._disk_available_bytes("/workspace"), 100 * 4096)

    def test_probe_failure_returns_bounded_conservative_snapshot(self) -> None:
        with patch_env({capacity.MAX_PARALLELISM_ENV: "10"}), mock.patch.object(
            capacity, "_collect_signals", side_effect=OSError("private detail must not leak")
        ):
            result = capacity.observe_capacity()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["capacity_parallelism_ceiling"], 1)
        self.assertEqual(result["reason_codes"], ["LIMIT_SIGNAL_UNKNOWN"])
        self.assertEqual(result["signals"], {"probe_status": "unavailable", "sampled_window_ms": 50})
        self.assertNotIn("private detail", repr(result))

    def test_signal_summary_is_bounded_and_contains_no_process_inventory(self) -> None:
        result = self._observe("2")
        self.assertEqual(
            set(result["signals"]),
            {
                "active_processors", "load1", "cpu_busy_pct", "sampled_window_ms", "thermal_state",
                "swap_used_bytes", "swap_total_bytes", "swapin_delta_pages", "swapout_delta_pages",
                "vm_free_bytes", "vm_inactive_bytes", "vm_purgeable_bytes", "vm_compressor_bytes",
                "disk_available_bytes",
            },
        )
        self.assertTrue({"processes", "process_inventory", "command_lines", "commands"}.isdisjoint(result["signals"]))


class patch_env:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self._patcher = mock.patch.dict(os.environ, values, clear=True)

    def __enter__(self):
        return self._patcher.__enter__()

    def __exit__(self, *args):
        return self._patcher.__exit__(*args)


if __name__ == "__main__":
    unittest.main()
