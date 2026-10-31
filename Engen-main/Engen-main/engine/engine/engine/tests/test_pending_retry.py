
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.signature_store import SignatureStore
from engine.solana_public import SolanaRPCError, SolanaRPCProviderError
from engine.transaction_monitor import monitor_wallet, process_new_signatures


class PendingRetryTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "pending_retry.db"
        self.store = SignatureStore(db_path=db_path)
        self.wallet = "11111111111111111111111111111111"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unavailable_signature_is_retried_until_processed(self):
        signature = "1111111111111111111111111111111111111111111111111111111111111113"

        with (
            patch(
                "engine.wallet_monitor.get_wallet_signatures"
            ) as mock_get_signatures,
            patch(
                "engine.transaction_monitor.process_transaction_signature"
            ) as mock_process,
        ):
            mock_get_signatures.side_effect = [
                [{"signature": signature}],
                [{"signature": signature}],
                [{"signature": signature}],
            ]

            mock_process.side_effect = [
                {
                    "signature": signature,
                    "available": False,
                    "error": "Transaction data is not available yet.",
                },
                {
                    "signature": signature,
                    "available": True,
                    "analysis": "completed",
                },
            ]

            # The first scan analyzes the historical baseline for History.
            # Its result remains pending when transaction metadata is unavailable.
            first_result = monitor_wallet(self.wallet, store=self.store)
            self.assertEqual(len(first_result), 1)
            self.assertTrue(first_result[0]["historical_baseline"])
            self.assertFalse(first_result[0]["available"])
            self.assertTrue(first_result[0]["retryable"])
            self.assertEqual(self.store.get_status(self.wallet, signature), "pending")
            self.assertEqual(mock_process.call_count, 1)

            # The next scan retries the same historical signature.
            second_result = monitor_wallet(self.wallet, store=self.store)
            self.assertEqual(len(second_result), 1)
            self.assertTrue(second_result[0]["available"])
            self.assertTrue(second_result[0]["historical_baseline"])
            self.assertEqual(self.store.get_status(self.wallet, signature), "processed")

            # A completed historical signature is not analyzed again.
            third_result = monitor_wallet(self.wallet, store=self.store)
            self.assertEqual(third_result, [])
            self.assertEqual(mock_process.call_count, 2)

    def test_provider_error_stops_after_five_attempts_and_needs_review(self):
        signature = "1" * 64

        with patch(
            "engine.transaction_monitor.process_transaction_signature",
            side_effect=SolanaRPCProviderError(-32015),
        ):
            for attempt in range(5):
                result = process_new_signatures(
                    [{"signature": signature}],
                    wallet_address=self.wallet,
                    store=self.store,
                )[0]
                if attempt < 4:
                    self.assertTrue(result["retryable"])
                    self.assertEqual(result["processing_status"], "pending")

        self.assertFalse(result["retryable"])
        self.assertEqual(result["rpc_code"], -32015)
        self.assertEqual(result["processing_status"], "needs_review")
        self.assertEqual(self.store.get_status(self.wallet, signature), "needs_review")
        self.assertEqual(self.store.get_pending_signatures(self.wallet), [])

    def test_rpc_error_keeps_signature_pending_for_retry(self):
        signature = "1111111111111111111111111111111111111111111111111111111111111114"

        with (
            patch(
                "engine.wallet_monitor.get_wallet_signatures"
            ) as mock_get_signatures,
            patch(
                "engine.transaction_monitor.process_transaction_signature"
            ) as mock_process,
        ):
            mock_get_signatures.side_effect = [
                [{"signature": "historical-signature"}],
                [{"signature": signature}],
                [{"signature": signature}],
            ]

            mock_process.side_effect = [
                SolanaRPCError("Temporary RPC failure."),
                {
                    "signature": signature,
                    "available": True,
                    "analysis": "completed",
                },
            ]

            # Establish the baseline.
            self.assertEqual(
                monitor_wallet(self.wallet, store=self.store),
                [],
            )

            # An RPC error should be reported without losing the signature.
            error_result = monitor_wallet(
                self.wallet,
                store=self.store,
            )

            self.assertEqual(len(error_result), 1)
            self.assertTrue(error_result[0]["retryable"])
            self.assertEqual(
                self.store.get_status(self.wallet, signature),
                "pending",
            )

            # A later scan should retry and successfully process it.
            success_result = monitor_wallet(
                self.wallet,
                store=self.store,
            )

            self.assertEqual(len(success_result), 1)
            self.assertTrue(success_result[0]["available"])
            self.assertEqual(
                self.store.get_status(self.wallet, signature),
                "processed",
            )
            self.assertEqual(mock_process.call_count, 2)


if __name__ == "__main__":
    unittest.main()
