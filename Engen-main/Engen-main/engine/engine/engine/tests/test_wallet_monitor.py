import json
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.signature_store import SignatureStore
from engine.solana_public import SolanaRPCError
from engine.wallet_monitor import (
    filter_new_signatures,
    get_and_store_new_wallet_signatures,
    get_new_wallet_signatures,
    get_wallet_signatures,
)


class WalletMonitorTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "monitor.db"
        self.store = SignatureStore(db_path=db_path)
        self.wallet = "11111111111111111111111111111111"

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_before_cursor_is_sent_to_rpc(self, mock_urlopen):
        response = mock_urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": [{"signature": "1111111111111111111111111111111111111111111111111111111111111112"}],
        }).encode("utf-8")

        result = get_wallet_signatures(
            wallet_address=self.wallet,
            limit=5,
            rpc_url="https://example.com",
            timeout=7,
            before="cursor-signature",
        )

        self.assertEqual(
            result,
            [{"signature": "1111111111111111111111111111111111111111111111111111111111111112"}],
        )

        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))

        self.assertEqual(request.full_url, "https://example.com")
        self.assertEqual(
            payload["params"],
            [
                self.wallet,
                {
                    "limit": 5,
                    "before": "cursor-signature",
                },
            ],
        )
        mock_urlopen.assert_called_once_with(request, timeout=7)

    def test_empty_before_cursor_is_rejected(self):
        with self.assertRaises(ValueError):
            get_wallet_signatures(
                wallet_address=self.wallet,
                before=" ",
            )

    def test_filter_returns_only_unseen_signatures(self):
        signatures = [
            {"signature": "sig_A"},
            {"signature": "sig_B"},
            {"signature": "sig_A"},
            {"signature": ""},
            None,
        ]

        result = filter_new_signatures(
            signatures,
            ["sig_B"],
        )

        self.assertEqual(
            result,
            [{"signature": "sig_A"}],
        )

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_get_new_wallet_signatures_uses_filter(
        self,
        mock_get_signatures,
    ):
        mock_get_signatures.return_value = [
            {"signature": "sig_A"},
            {"signature": "sig_B"},
        ]

        result = get_new_wallet_signatures(
            wallet_address=self.wallet,
            seen_signatures=["sig_A"],
            limit=5,
            rpc_url="https://example.com",
            timeout=7,
        )

        self.assertEqual(
            result,
            [{"signature": "sig_B"}],
        )
        mock_get_signatures.assert_called_once_with(
            wallet_address=self.wallet,
            limit=5,
            rpc_url="https://example.com",
            timeout=7,
        )

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_first_scan_stores_baseline_without_new_signals(
        self,
        mock_get_signatures,
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111113"},
            {"signature": "1111111111111111111111111111111111111111111111111111111111111114"},
        ]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        self.assertEqual(result, [])
        self.assertTrue(self.store.has_baseline(self.wallet))
        self.assertTrue(self.store.has_seen(self.wallet, "1111111111111111111111111111111111111111111111111111111111111113"))
        self.assertTrue(self.store.has_seen(self.wallet, "1111111111111111111111111111111111111111111111111111111111111114"))

        self.assertEqual(
            self.store.get_status(self.wallet, "1111111111111111111111111111111111111111111111111111111111111113"),
            "processed",
        )
        self.assertEqual(
            self.store.get_status(self.wallet, "1111111111111111111111111111111111111111111111111111111111111114"),
            "processed",
        )

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_repeated_scan_does_not_return_saved_signatures(
        self,
        mock_get_signatures,
    ):
        self.store.mark_baseline(self.wallet)
        self.store.save(self.wallet, "sig_A")
        self.store.mark_processed(self.wallet, "sig_A")

        mock_get_signatures.return_value = [
            {"signature": "sig_A"},
        ]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        self.assertEqual(result, [])
        self.assertEqual(
            self.store.get_status(self.wallet, "sig_A"),
            "processed",
        )

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_new_signature_is_returned_after_baseline(
        self,
        mock_get_signatures,
    ):
        self.store.mark_baseline(self.wallet)

        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111115"},
        ]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        self.assertEqual(
            result,
            [{"signature": "1111111111111111111111111111111111111111111111111111111111111115"}],
        )
        self.assertEqual(
            self.store.get_status(self.wallet, "1111111111111111111111111111111111111111111111111111111111111115"),
            "pending",
        )

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_saved_pending_signature_is_returned_again(
        self,
        mock_get_signatures,
    ):
        self.store.mark_baseline(self.wallet)
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111115"},
        ]

        first_result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        second_result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        expected = [{"signature": "1111111111111111111111111111111111111111111111111111111111111115"}]

        self.assertEqual(first_result, expected)
        self.assertEqual(second_result, expected)
        self.assertEqual(
            self.store.get_status(self.wallet, "1111111111111111111111111111111111111111111111111111111111111115"),
            "pending",
        )


    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_rpc_errors_do_not_expose_configured_url_or_credentials(
        self,
        mock_urlopen,
    ):
        mock_urlopen.side_effect = urllib.error.URLError(
            "https://rpc.example/?api-key=super-secret"
        )

        with self.assertRaisesRegex(
            SolanaRPCError,
            "Public Solana RPC request failed\\.",
        ) as context:
            get_wallet_signatures(
                wallet_address=self.wallet,
                rpc_url="https://rpc.example/?api-key=super-secret",
            )

        self.assertNotIn("super-secret", str(context.exception))
        self.assertNotIn("rpc.example", str(context.exception))


if __name__ == "__main__":
    unittest.main()