
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from engine.solana_public import (
    SolanaRPCError,
    SolanaRPCHTTPError,
    SolanaRPCProviderError,
    get_transaction,
)


class TransactionRPCTests(unittest.TestCase):

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_returns_transaction_data(self, mock_urlopen):
        transaction = {
            "slot": 123,
            "meta": {"err": None, "fee": 5000},
        }

        response = MagicMock()
        response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 4, "result": transaction}
        ).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        result = get_transaction("1111111111111111111111111111111111111111111111111111111111111111")

        self.assertEqual(result, transaction)
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["params"][1]["maxSupportedTransactionVersion"], 0)

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_returns_none_when_transaction_is_not_found(
        self, mock_urlopen
    ):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 4, "result": None}
        ).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        self.assertIsNone(get_transaction("1111111111111111111111111111111111111111111111111111111111111112"))

    def test_malformed_signature_is_rejected_before_network_call(self):
        with patch("engine.rpc_policy.urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(ValueError):
                get_transaction("not-a-valid-signature")
            mock_urlopen.assert_not_called()

    def test_empty_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            get_transaction("")

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_rpc_error_raises_exception(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 4,
            "error": {
                "code": -1,
                "message": "Test error",
            }
        }).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(SolanaRPCProviderError):
            get_transaction("1111111111111111111111111111111111111111111111111111111111111111")

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_invalid_result_is_rejected(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 4, "result": "invalid"}
        ).encode("utf-8")

        mock_urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(SolanaRPCError):
            get_transaction("1111111111111111111111111111111111111111111111111111111111111111")


    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_http_error_is_distinct_from_provider_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://rpc.example", 404, "not found", {}, None
        )
        with self.assertRaises(SolanaRPCHTTPError) as caught:
            get_transaction("1111111111111111111111111111111111111111111111111111111111111111")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertNotIn("rpc.example", str(caught.exception))

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_mismatched_json_rpc_id_is_rejected(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 999, "result": None,
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(SolanaRPCError, "mismatched"):
            get_transaction("1111111111111111111111111111111111111111111111111111111111111111")

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_network_error_does_not_expose_rpc_url_credentials(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(
            "https://rpc.example/?api-key=super-secret"
        )

        with self.assertRaises(SolanaRPCError) as context:
            get_transaction(
                "1111111111111111111111111111111111111111111111111111111111111111",
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
