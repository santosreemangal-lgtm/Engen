"""Offline integration tests for the paper-pipeline latency API."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from unittest.mock import patch

from dashboard import monitor_bridge, server as dashboard_server
from engine.latency import LatencyRecorder


WALLET = "11111111111111111111111111111111"


class PaperLatencyIntegrationTests(unittest.TestCase):
    def test_wallet_scan_samples_are_exposed_by_latency_http_api(self):
        recorder = LatencyRecorder(max_samples=100)
        observed = [{
            "signature": "integration-test-signature",
            "retryable": False,
            "trade_signal": {"classification": "UNKNOWN"},
        }]

        class EmptyPaperSimulator:
            orders = {}
            balances = {}
            processed_signals = set()

        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard_server.DashboardHandler
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(monitor_bridge, "LATENCY_RECORDER", recorder), \
                 patch.object(dashboard_server, "LATENCY_RECORDER", recorder), \
                 patch.object(monitor_bridge, "SignatureStore"), \
                 patch.object(monitor_bridge, "monitor_wallet", return_value=observed), \
                 patch.object(monitor_bridge, "queue_paper_orders", return_value=[]), \
                 patch.object(monitor_bridge, "execute_pending_paper_orders", return_value=[]):
                scan = monitor_bridge.create_scan_function(
                    simulator_getter=EmptyPaperSimulator
                )
                result = scan(WALLET)
                self.assertEqual(result, observed)

                endpoint = "http://127.0.0.1:%s/api/latency" % httpd.server_port
                with urlopen(endpoint, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers.get("Cache-Control"), "no-store"
                    )
                    payload = json.loads(response.read().decode("utf-8"))

            self.assertEqual(payload["mode"], "PAPER_ONLY")
            self.assertFalse(payload["live_execution_latency"])
            self.assertFalse(payload["transaction_broadcast"])
            for stage in (
                "pipeline.scan_total",
                "pipeline.profile_persistence",
                "paper.order_queue",
                "paper.order_execution",
            ):
                with self.subTest(stage=stage):
                    self.assertIn(stage, payload["samples"])
                    self.assertEqual(payload["samples"][stage]["count"], 1)
                    self.assertGreaterEqual(
                        payload["samples"][stage]["mean_ms"], 0
                    )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
