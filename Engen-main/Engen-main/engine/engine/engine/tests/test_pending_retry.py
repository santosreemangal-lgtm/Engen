
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.signature_store import SignatureStore
from engine.solana_public import SolanaRPCError
from engine.transaction_monitor import monitor_wallet


class PendingRetryTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "pending_retry.db"
        self.store = SignatureStore(db_path=db_path)
        self.wallet = "test_public_wallet"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unavailable_signature_is_retried_until_processed(self):
        signature = "new-signature"

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

            # First scan establishes the historical baseline.
            first_result = monitor_wallet(
                self.wallet,
                store=self.store,
            )

            self.assertEqual(first_result, [])
            self.assertEqual(mock_process.call_count, 0)

            # Second scan finds the new signature, but its transaction
            # data is temporarily unavailable.
            second_result = monitor_wallet(
                self.wallet,
                store=self.store,
            )

            self.assertEqual(len(second_result), 1)
            self.assertFalse(second_result[0]["available"])
            self.assertTrue(second_result[0]["retryable"])
            self.assertEqual(
                self.store.get_status(self.wallet, signature),
                "pending",
            )

            # Third scan retries the pending signature.
            third_result = monitor_wallet(
                self.wallet,
                store=self.store,
            )

            self.assertEqual(len(third_result), 1)
            self.assertTrue(third_result[0]["available"])
            self.assertEqual(
                self.store.get_status(self.wallet, signature),
                "processed",
            )

            # Fourth scan must not process the same signature again.
            fourth_result = monitor_wallet(
                self.wallet,
                store=self.store,
            )

            self.assertEqual(fourth_result, [])
            self.assertEqual(mock_process.call_count, 2)

    def test_rpc_error_keeps_signature_pending_for_retry(self):
        signature = "retry-after-rpc-error"

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
