
import sqlite3
import subprocess
import sys
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

    def test_state_survives_a_full_python_process_restart(self):
        db_path = Path(self.temp_dir.name) / "process_restart.db"
        first_process = (
            "from engine.signature_store import SignatureStore; "
            "s=SignatureStore(db_path=r'" + str(db_path) + "'); "
            "s.save('wallet_restart', 'sig_persisted'); "
            "s.mark_processed('wallet_restart', 'sig_persisted')"
        )
        subprocess.run(
            [sys.executable, "-c", first_process],
            check=True,
            capture_output=True,
            text=True,
        )

        # A separate interpreter simulates the application process restarting.
        second_process = (
            "from engine.signature_store import SignatureStore; "
            "s=SignatureStore(db_path=r'" + str(db_path) + "'); "
            "assert s.has_seen('wallet_restart', 'sig_persisted'); "
            "assert s.get_status('wallet_restart', 'sig_persisted') == 'processed'; "
            "assert s.save('wallet_restart', 'sig_persisted') is False; "
            "print('restart-persistence-and-deduplication: PASS')"
        )
        result = subprocess.run(
            [sys.executable, "-c", second_process],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("restart-persistence-and-deduplication: PASS", result.stdout)

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

    def test_failure_retries_are_bounded_and_reviewable(self):
        self.store.save("wallet_A", "sig_fail")
        for _ in range(4):
            result = self.store.record_failure(
                "wallet_A", "sig_fail", "rpc_fetch",
                "temporary_rpc_error", "provider temporarily unavailable",
            )
            self.assertEqual(result["status"], "pending")
        result = self.store.record_failure(
            "wallet_A", "sig_fail", "rpc_fetch",
            "temporary_rpc_error", "provider temporarily unavailable",
        )
        self.assertEqual(result, {"status": "needs_review", "attempts": 5})
        self.assertEqual(self.store.get_pending_signatures("wallet_A"), [])
        error = self.store.get_error_record("wallet_A", "sig_fail")
        self.assertEqual(error["error_stage"], "rpc_fetch")
        self.assertEqual(error["attempts"], 5)

    def test_transaction_record_is_idempotent_and_preserves_public_payload(self):
        payload = {"slot": 42, "meta": {"fee": 5000}}
        self.store.save_transaction_record(
            "wallet_A", "sig_public", payload, "observed"
        )
        self.store.save_transaction_record(
            "wallet_A", "sig_public", None, "incomplete"
        )
        record = self.store.get_transaction_record("wallet_A", "sig_public")
        self.assertEqual(record["raw_transaction"], payload)
        self.assertEqual(record["status"], "incomplete")

    def test_processed_slot_updates_wallet_position_monotonically(self):
        self.store.save("wallet_A", "sig_newer", slot=200)
        self.store.mark_processed("wallet_A", "sig_newer")
        self.store.save("wallet_A", "sig_older", slot=190)
        self.store.mark_processed("wallet_A", "sig_older")
        position = self.store.get_position("wallet_A")
        self.assertEqual(position["slot"], 200)
        self.assertEqual(position["signature"], "sig_newer")

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

    def test_save_many_rolls_back_entire_batch_on_database_failure(self):
        with sqlite3.connect(self.store.db_path) as connection:
            connection.execute("""
                CREATE TRIGGER reject_one_signature
                BEFORE INSERT ON seen_signatures
                WHEN NEW.signature = 'sig_force_failure'
                BEGIN
                    SELECT RAISE(ABORT, 'forced batch failure');
                END
            """)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_many(
                "wallet_A",
                ["sig_batch_one", "sig_force_failure", "sig_batch_three"],
            )

        self.assertFalse(self.store.has_seen("wallet_A", "sig_batch_one"))
        self.assertFalse(self.store.has_seen("wallet_A", "sig_force_failure"))
        self.assertFalse(self.store.has_seen("wallet_A", "sig_batch_three"))

    def test_concurrent_duplicate_signature_saves_are_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading

        barrier = threading.Barrier(8)

        def save_one(_):
            barrier.wait()
            return self.store.save("wallet_concurrent", "same_signature")

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(save_one, range(8)))

        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(
            self.store.get_status("wallet_concurrent", "same_signature"),
            "pending",
        )


if __name__ == "__main__":
    unittest.main()
