import json
import unittest
from unittest.mock import MagicMock, patch

from engine.solana_public import (
    SolanaRPCError,
    get_latest_blockhash,
)


class BlockhashTests(unittest.TestCase):

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_returns_blockhash(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "result": {
                "value": {
                    "blockhash": "test_blockhash_123"
                }
            }
        }).encode()

        mock_urlopen.return_value.__enter__.return_value = response

        result = get_latest_blockhash()

        self.assertEqual(result, "test_blockhash_123")

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_rpc_error_raises_exception(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "error": {
                "code": -1,
                "message": "Test error"
            }
        }).encode()

        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(SolanaRPCError):
            get_latest_blockhash()


if __name__ == "__main__":
    unittest.main()