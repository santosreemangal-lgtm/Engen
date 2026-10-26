
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.signature_store import SignatureStore


class SignatureStoreTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test_signatures.db"
        self.store = SignatureStore(db_path=db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_new_signature(self):
        saved = self.store.save("wallet_A", "sig_123")

        self.assertTrue(saved)
        self.assertTrue(self.store.has_seen("wallet_A", "sig_123"))

    def test_duplicate_signature_is_not_saved_twice(self):
        first_save = self.store.save("wallet_A", "sig_123")
        second_save = self.store.save("wallet_A", "sig_123")

        self.assertTrue(first_save)
        self.assertFalse(second_save)

    def test_wallets_are_kept_separate(self):
        self.store.save("wallet_A", "sig_123")

        self.assertTrue(self.store.has_seen("wallet_A", "sig_123"))
        self.assertFalse(self.store.has_seen("wallet_B", "sig_123"))

    def test_save_many_counts_only_new_signatures(self):
        saved = self.store.save_many(
            "wallet_A",
            ["sig_1", "sig_2", "sig_1"],
        )

        self.assertEqual(saved, 2)
        self.assertEqual(
            set(self.store.get_seen_signatures("wallet_A")),
            {"sig_1", "sig_2"},
        )

    def test_invalid_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.get_seen_signatures("wallet_A", limit=0)

    def test_empty_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save("wallet_A", "")

    def test_new_signature_starts_as_pending(self):
        self.store.save("wallet_A", "sig_pending")

        self.assertEqual(
            self.store.get_status("wallet_A", "sig_pending"),
            "pending",
        )

    def test_signature_can_be_marked_processed(self):
        self.store.save("wallet_A", "sig_processed")

        result = self.store.mark_processed(
            "wallet_A",
            "sig_processed",
        )

        self.assertTrue(result)
        self.assertEqual(
            self.store.get_status("wallet_A", "sig_processed"),
            "processed",
        )

    def test_pending_list_excludes_processed_signatures(self):
        self.store.save("wallet_A", "sig_pending")
        self.store.save("wallet_A", "sig_processed")

        self.store.mark_processed("wallet_A", "sig_processed")

        pending = self.store.get_pending_signatures("wallet_A")

        self.assertEqual(pending, ["sig_pending"])

    def test_legacy_database_migration_marks_existing_signatures_processed(
        self,
    ):
        legacy_db_path = (
            Path(self.temp_dir.name) / "legacy_signatures.db"
        )

        # Build a database using the previous schema, without a status column.
        connection = sqlite3.connect(legacy_db_path)

        try:
            connection.execute(
                """
                CREATE TABLE seen_signatures (
                    wallet_address TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (wallet_address, signature)
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE wallet_baselines (
                    wallet_address TEXT PRIMARY KEY,
                    initialized_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            connection.execute(
                """
                INSERT INTO seen_signatures
                    (wallet_address, signature)
                VALUES (?, ?)
                """,
                ("wallet_A", "legacy_sig"),
            )

            connection.execute(
                """
                INSERT INTO wallet_baselines (wallet_address)
                VALUES (?)
                """,
                ("wallet_A",),
            )

            connection.commit()
        finally:
            connection.close()

        # Opening the legacy database should add the status column safely.
        migrated_store = SignatureStore(db_path=legacy_db_path)

        self.assertTrue(migrated_store.has_baseline("wallet_A"))
        self.assertTrue(migrated_store.has_seen("wallet_A", "legacy_sig"))
        self.assertEqual(
            migrated_store.get_status("wallet_A", "legacy_sig"),
            "processed",
        )
        self.assertEqual(
            migrated_store.get_pending_signatures("wallet_A"),
            [],
        )

        # Newly discovered signatures should still start as pending.
        migrated_store.save("wallet_A", "new_sig")

        self.assertEqual(
            migrated_store.get_status("wallet_A", "new_sig"),
            "pending",
        )
        self.assertEqual(
            migrated_store.get_pending_signatures("wallet_A"),
            ["new_sig"],
        )


if __name__ == "__main__":
    unittest.main()
