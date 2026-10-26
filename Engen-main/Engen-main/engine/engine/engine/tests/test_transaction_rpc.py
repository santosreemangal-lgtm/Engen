
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from engine.solana_public import (
    SolanaRPCError,
    get_transaction,
)


class TransactionRPCTests(unittest.TestCase):

    @patch("engine.solana_public.urllib.request.urlopen")
    def test_returns_transaction_data(self, mock_urlopen):
        transaction = {
            "slot": 123,
            "meta": {"err": None, "fee": 5000},
        }

        response = MagicMock()
        response.read.return_value = json.dumps(
            {"result": transaction}
        ).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        result = get_transaction("test_signature")

        self.assertEqual(result, transaction)

    @patch("engine.solana_public.urllib.request.urlopen")
    def test_returns_none_when_transaction_is_not_found(
        self, mock_urlopen
    ):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"result": None}
        ).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        self.assertIsNone(get_transaction("unknown_signature"))

    def test_empty_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            get_transaction("")

    @patch("engine.solana_public.urllib.request.urlopen")
    def test_rpc_error_raises_exception(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "error": {
                "code": -1,
                "message": "Test error",
            }
        }).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(SolanaRPCError):
            get_transaction("test_signature")

    @patch("engine.solana_public.urllib.request.urlopen")
    def test_invalid_result_is_rejected(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"result": "invalid"}
        ).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(SolanaRPCError):
            get_transaction("test_signature")


    @patch("engine.solana_public.urllib.request.urlopen")
    def test_network_error_does_not_expose_rpc_url_credentials(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(
            "https://rpc.example/?api-key=super-secret"
        )

        with self.assertRaises(SolanaRPCError) as context:
            get_transaction(
                "test_signature",
                rpc_url="https://rpc.example/?api-key=super-secret",
            )

        self.assertEqual(
            str(context.exception),
            "Public Solana RPC request failed.",
        )
        self.assertIsNone(context.exception.__cause__)
        self.assertTrue(context.exception.__suppress_context__)
        self.assertNotIn("super-secret", str(context.exception))


if __name__ == "__main__":
    unittest.main()
