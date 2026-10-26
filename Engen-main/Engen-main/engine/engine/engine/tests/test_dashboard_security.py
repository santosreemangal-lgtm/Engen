import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import patch

from dashboard import server


class DashboardWalletValidationTests(unittest.TestCase):
    def test_accepts_valid_32_byte_base58_public_key(self):
        for address in (
            "11111111111111111111111111111111",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        ):
            with self.subTest(address=address):
                self.assertTrue(server.is_valid_solana_address(address))

    def test_rejects_wrong_decoded_length(self):
        self.assertFalse(
            server.is_valid_solana_address(
                "1111111111111111111111111111111"
            )
        )

    def test_rejects_invalid_base58_alphabet(self):
        self.assertFalse(
            server.is_valid_solana_address(
                "0OIl11111111111111111111111111111"
            )
        )

    def test_rejects_non_string_values(self):
        for value in (None, 123, [], {}):
            with self.subTest(value=value):
                self.assertFalse(server.is_valid_solana_address(value))

    def test_load_wallets_filters_invalid_entries_and_caps_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            valid = "11111111111111111111111111111111"
            path.write_text(
                json.dumps([valid, "not-a-public-key", valid]),
                encoding="utf-8",
            )
            with patch.object(server, "WALLETS_FILE", path):
                self.assertEqual(server.load_wallets(), [valid])


if __name__ == "__main__":
    unittest.main()


class DashboardSecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s/api/monitor" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_monitor_api_includes_browser_security_headers(self):
        with urlopen(self.url, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
            self.assertIn("default-src 'self'", response.headers.get("Content-Security-Policy", ""))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_dashboard_disables_unneeded_browser_capabilities(self):
        with urlopen(self.url, timeout=2) as response:
            policy = response.headers.get("Permissions-Policy", "")
            self.assertIn("camera=()", policy)
            self.assertIn("microphone=()", policy)
            self.assertIn("geolocation=()", policy)

    def test_post_rejects_non_json_content_type(self):
        request = Request(
            self.url.replace("/api/monitor", "/api/wallets"),
            data=b"{}",
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 415)
        self.assertIn("application/json", caught.exception.read().decode("utf-8"))

    def test_post_rejects_malformed_json(self):
        request = Request(
            self.url.replace("/api/monitor", "/api/wallets"),
            data=b"{not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)
