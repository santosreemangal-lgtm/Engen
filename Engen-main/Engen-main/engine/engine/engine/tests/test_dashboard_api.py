import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from dashboard import server as dashboard_server
from engine.paper_trading import PaperTrader


class DashboardPaperApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.wallets_file_patch = patch.object(
            dashboard_server, "WALLETS_FILE", Path(self.temp_dir.name) / "wallets.json"
        )
        self.wallets_file_patch.start()
        self.trader_patch = patch.object(dashboard_server, "paper_trader", PaperTrader())
        self.trader_patch.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%s" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.trader_patch.stop()
        self.wallets_file_patch.stop()
        self.temp_dir.cleanup()

    def request_json(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="GET" if data is None else "POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_paper_status_and_history_are_available(self):
        status, body = self.request_json("/api/paper/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "paper")
        self.assertFalse(body["live_trading_enabled"])
        status, body = self.request_json("/api/paper/history")
        self.assertEqual(status, 200)
        self.assertEqual(body["history"], [])

    def test_paper_trade_endpoint_updates_virtual_balance(self):
        status, body = self.request_json(
            "/api/paper/trade",
            {"side": "buy", "token": "TEST_TOKEN", "quantity": 1, "price": 10},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["trade"]["side"], "buy")
        self.assertEqual(body["status"]["cash"], 990)
        self.assertFalse(body["status"]["live_trading_enabled"])

    def test_invalid_trade_returns_client_error(self):
        status, body = self.request_json(
            "/api/paper/trade",
            {"side": "buy", "token": "TEST_TOKEN", "quantity": 0, "price": 10},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_paper_pause_and_resume_controls(self):
        status, body = self.request_json("/api/paper/control", {"action": "pause"})
        self.assertEqual(status, 200)
        self.assertTrue(body["paused"])
        status, body = self.request_json("/api/paper/control", {"action": "resume"})
        self.assertEqual(status, 200)
        self.assertFalse(body["paused"])


if __name__ == "__main__":
    unittest.main()
