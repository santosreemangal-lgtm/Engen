import unittest
from unittest.mock import patch

from engine.latency import LatencyRecorder


class LatencyRecorderTests(unittest.TestCase):
    def test_records_explicit_samples_and_percentiles(self):
        recorder = LatencyRecorder()
        for value in (1, 2, 3, 4, 100):
            recorder.record("analysis", value)

        summary = recorder.summary()["analysis"]
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["mean_ms"], 22.0)
        self.assertEqual(summary["max_ms"], 100.0)
        self.assertEqual(summary["p50_ms"], 3.0)
        self.assertEqual(summary["p95_ms"], 100.0)
        self.assertEqual(summary["p99_ms"], 100.0)

    def test_samples_are_bounded(self):
        recorder = LatencyRecorder(max_samples=2)
        recorder.record("rpc", 1)
        recorder.record("rpc", 2)
        recorder.record("rpc", 3)
        self.assertEqual(recorder.summary()["rpc"]["count"], 2)
        self.assertEqual(recorder.summary()["rpc"]["max_ms"], 2.0)

    def test_measure_context_records_duration(self):
        recorder = LatencyRecorder()
        with patch("engine.latency.perf_counter_ns", side_effect=[1_000_000, 4_500_000]):
            with recorder.measure("decode"):
                pass
        self.assertEqual(recorder.summary()["decode"]["max_ms"], 3.5)

    def test_rejects_invalid_capacity(self):
        for value in (0, -1, True, 1.5, "10"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    LatencyRecorder(max_samples=value)

    def test_rejects_invalid_stage_and_duration(self):
        recorder = LatencyRecorder()
        for stage in ("", "  ", None):
            with self.subTest(stage=stage):
                with self.assertRaises(ValueError):
                    recorder.record(stage, 1)
        for duration in (-1, True, "1", float("nan")):
            with self.subTest(duration=duration):
                with self.assertRaises((ValueError, TypeError)):
                    recorder.record("rpc", duration)

    def test_empty_summary_is_empty(self):
        self.assertEqual(LatencyRecorder().summary(), {})


if __name__ == "__main__":
    unittest.main()
