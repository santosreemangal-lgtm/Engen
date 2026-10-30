import unittest
from unittest.mock import patch

from engine.latency import LatencyRecorder, LATENCY_RECORDER


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

    def test_nearest_rank_percentiles_use_ceiling_rank(self):
        recorder = LatencyRecorder()
        for value in (1, 2, 3, 4):
            recorder.record("analysis", value)

        summary = recorder.summary()["analysis"]
        self.assertEqual(summary["p50_ms"], 2.0)
        self.assertEqual(summary["p95_ms"], 4.0)
        self.assertEqual(summary["p99_ms"], 4.0)

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


    def test_dashboard_scan_records_end_to_end_paper_stages(self):
        from dashboard.monitor_bridge import create_scan_function

        class EmptySimulator:
            orders = {}
            balances = {}
            processed_signals = set()

        with patch("dashboard.monitor_bridge.SignatureStore"), \
             patch("dashboard.monitor_bridge.monitor_wallet", return_value=[]), \
             patch("dashboard.monitor_bridge.queue_paper_orders", return_value=[]), \
             patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]):
            scan = create_scan_function(simulator_getter=EmptySimulator)
            self.assertEqual(scan("11111111111111111111111111111111"), [])

        samples = LATENCY_RECORDER.summary()
        for stage in ("pipeline.scan_total", "paper.order_queue", "paper.order_execution"):
            self.assertIn(stage, samples)
            self.assertGreaterEqual(samples[stage]["count"], 1)

    def test_empty_summary_is_empty(self):
        self.assertEqual(LatencyRecorder().summary(), {})


if __name__ == "__main__":
    unittest.main()
