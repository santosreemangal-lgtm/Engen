
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.signature_store import SignatureStore
from engine.wallet_monitor import get_and_store_new_wallet_signatures


class MonitorBaselineTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "baseline.db"
        self.store = SignatureStore(db_path=self.db_path)
        self.wallet = "11111111111111111111111111111111"

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_first_scan_returns_historical_entries_for_analysis(self, mock_get_signatures):
        signatures = [
            "1111111111111111111111111111111111111111111111111111111111111112",
            "1111111111111111111111111111111111111111111111111111111111111113",
        ]
        mock_get_signatures.return_value = [{"signature": value} for value in signatures]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet, store=self.store,
        )

        self.assertEqual({item["signature"] for item in result}, set(signatures))
        self.assertTrue(all(item.get("historical_baseline") is True for item in result))
        self.assertTrue(self.store.has_baseline(self.wallet))
        for signature in signatures:
            self.assertTrue(self.store.has_seen(self.wallet, signature))
            self.assertTrue(self.store.is_historical_baseline(self.wallet, signature))
            self.assertEqual(self.store.get_status(self.wallet, signature), "pending")

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_repeated_scan_does_not_return_saved_signatures(
        self, mock_get_signatures
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
        ]

        first_result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        # Simulate the bridge completing the first historical analysis.
        self.assertEqual(len(first_result), 1)
        self.assertTrue(first_result[0]["historical_baseline"])
        self.store.mark_processed(
            self.wallet, first_result[0]["signature"]
        )
        second_result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet, store=self.store,
        )

        self.assertEqual(second_result, [])

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_new_signature_is_returned_after_baseline(
        self, mock_get_signatures
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
        ]

        initial = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet, store=self.store,
        )
        self.assertEqual(len(initial), 1)
        self.assertTrue(initial[0]["historical_baseline"])
        self.store.mark_processed(self.wallet, initial[0]["signature"])

        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111114"},
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
        ]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        self.assertEqual(
            [item["signature"] for item in result],
            ["1111111111111111111111111111111111111111111111111111111111111114"],
        )
        self.assertTrue(self.store.has_seen(self.wallet, "1111111111111111111111111111111111111111111111111111111111111114"))

    def test_older_processed_baseline_without_transaction_record_is_requeued_once(self):
        signature = "1111111111111111111111111111111111111111111111111111111111111112"
        self.store.mark_baseline(self.wallet)
        self.store.save(self.wallet, signature, slot=100)
        self.store.mark_processed(self.wallet, signature)
        self.assertEqual(self.store.get_status(self.wallet, signature), "processed")

        migrated_store = SignatureStore(db_path=self.db_path)

        self.assertEqual(migrated_store.get_status(self.wallet, signature), "pending")
        self.assertTrue(migrated_store.is_historical_baseline(self.wallet, signature))
        self.assertEqual(migrated_store.get_pending_signatures(self.wallet), [signature])
        # Once tagged, a subsequent restart does not reset its processing state.
        migrated_store.mark_processed(self.wallet, signature)
        reopened_again = SignatureStore(db_path=self.db_path)
        self.assertEqual(reopened_again.get_status(self.wallet, signature), "processed")

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_baseline_persists_after_store_reopens(
        self, mock_get_signatures
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
        ]

        initial = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet, store=self.store,
        )
        self.assertEqual(len(initial), 1)
        self.store.mark_processed(self.wallet, initial[0]["signature"])

        reopened_store = SignatureStore(db_path=self.db_path)

        self.assertTrue(reopened_store.has_baseline(self.wallet))

        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
            {"signature": "1111111111111111111111111111111111111111111111111111111111111114"},
        ]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=reopened_store,
        )

        self.assertEqual(
            [item["signature"] for item in result],
            ["1111111111111111111111111111111111111111111111111111111111111114"],
        )


if __name__ == "__main__":
    unittest.main()
