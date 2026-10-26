import json
import unittest
from unittest.mock import MagicMock, patch

from engine.solana_public import SolanaRPCError, get_public_health, get_slot


class SolanaPublicTests(unittest.TestCase):
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_health_returns_ok(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"result": "ok"}).encode()
        mock_urlopen.return_value.__enter__.return_value = response
        self.assertEqual(get_public_health(), "ok")

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_slot_returns_number(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"result": 12345}).encode()
        mock_urlopen.return_value.__enter__.return_value = response
        self.assertEqual(get_slot(), 12345)

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_rpc_error_raises_exception(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"error": {"code": -1, "message": "Test error"}}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = response
        with self.assertRaises(SolanaRPCError):
            get_slot()


if __name__ == "__main__":
    unittest.main()