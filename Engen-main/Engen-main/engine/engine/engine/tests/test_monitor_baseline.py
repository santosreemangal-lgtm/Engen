
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
    def test_first_scan_stores_existing_signatures_but_returns_none(
        self, mock_get_signatures
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
            {"signature": "1111111111111111111111111111111111111111111111111111111111111113"},
        ]

        result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        self.assertEqual(result, [])
        self.assertTrue(self.store.has_seen(self.wallet, "1111111111111111111111111111111111111111111111111111111111111112"))
        self.assertTrue(self.store.has_seen(self.wallet, "1111111111111111111111111111111111111111111111111111111111111113"))
        self.assertTrue(self.store.has_baseline(self.wallet))

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

        second_result = get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

        self.assertEqual(first_result, [])
        self.assertEqual(second_result, [])

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_new_signature_is_returned_after_baseline(
        self, mock_get_signatures
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
        ]

        get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

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

    @patch("engine.wallet_monitor.get_wallet_signatures")
    def test_baseline_persists_after_store_reopens(
        self, mock_get_signatures
    ):
        mock_get_signatures.return_value = [
            {"signature": "1111111111111111111111111111111111111111111111111111111111111112"},
        ]

        get_and_store_new_wallet_signatures(
            wallet_address=self.wallet,
            store=self.store,
        )

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
