import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.profile_store import ProfileStore

PUBLIC_ADDRESS = "11111111111111111111111111111111"


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "profiles.db"
        self.store = ProfileStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_settings_are_isolated(self):
        a = self.store.create_profile("Alpha", settings={"risk": "low"})
        b = self.store.create_profile("Beta", settings={"risk": "medium"})
        self.store.set_settings(a, {"risk": "high"})
        self.assertEqual(self.store.get_profile(a)["settings"]["risk"], "high")
        self.assertEqual(self.store.get_profile(b)["settings"]["risk"], "medium")
        self.assertEqual(self.store.get_profile(a)["status"], "disabled")

    def test_wallet_can_be_shared_across_profiles(self):
        a = self.store.create_profile("Alpha")
        b = self.store.create_profile("Beta")
        self.store.associate_source_wallet(a, PUBLIC_ADDRESS)
        self.store.associate_source_wallet(b, PUBLIC_ADDRESS)
        self.assertEqual(self.store.get_profile(a)["source_wallets"], [PUBLIC_ADDRESS])
        self.assertEqual(self.store.get_profile(b)["source_wallets"], [PUBLIC_ADDRESS])

    def test_destination_profile_limit_is_enforced(self):
        self.store.create_profile("One")
        self.store.create_profile("Two")
        self.store.create_profile("Three")
        with self.assertRaisesRegex(ValueError, "maximum of 3 destination wallet profiles"):
            self.store.create_profile("Four")

    def test_unassigning_wallet_only_removes_selected_profile_association(self):
        first = self.store.create_profile("First")
        second = self.store.create_profile("Second")
        self.store.associate_source_wallet(first, PUBLIC_ADDRESS)
        self.store.associate_source_wallet(second, PUBLIC_ADDRESS)
        self.store.disassociate_source_wallet(first, PUBLIC_ADDRESS)
        self.assertEqual(self.store.get_profile(first)["source_wallets"], [])
        self.assertEqual(self.store.get_profile(second)["source_wallets"], [PUBLIC_ADDRESS])

    def test_secret_setting_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_profile("Unsafe", settings={"private_key": "x"})
        with self.assertRaises(ValueError):
            self.store.create_profile("Nested", settings={"wallet": {"seed_phrase": "x"}})

    def test_duplicate_transaction_signature_is_unique(self):
        a = self.store.create_profile("Alpha")
        self.store.associate_source_wallet(a, PUBLIC_ADDRESS)
        args = dict(signature="sample-signature", source_wallet_address=PUBLIC_ADDRESS,
                    classification="BUY_CANDIDATE", status="confirmed",
                    evidence={"reason": "manual review needed"}, raw_amount="000123",
                    token_mint=PUBLIC_ADDRESS, token_decimals=6, profile_ids=[a],
                    observed_at="2026-01-02T03:04:05Z")
        self.store.save_transaction(**args)
        self.store.save_transaction(**args)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_transactions").fetchone()[0], 1)
            row = db.execute("SELECT raw_amount,token_decimals,observed_at FROM transactions").fetchone()
        self.assertEqual(row, ("000123", 6, "2026-01-02T03:04:05Z"))

    def test_wallet_activity_retains_latest_1000_across_all_profile_wallets(self):
        profile = self.store.create_profile("Retention")
        second_wallet = "So11111111111111111111111111111111111111112"
        self.store.associate_source_wallet(profile, PUBLIC_ADDRESS)
        self.store.associate_source_wallet(profile, second_wallet)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)

        for index in range(600):
            observed = (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
            self.store.save_transaction(
                signature=f"wallet-a-{index:04d}",
                source_wallet_address=PUBLIC_ADDRESS,
                classification="UNKNOWN",
                status="observed",
                slot=10000 + index,
                block_time=1700000000 + index,
                profile_ids=[profile],
                observed_at=observed,
            )

        for index in range(600):
            sequence = 600 + index
            observed = (start + timedelta(seconds=sequence)).isoformat().replace("+00:00", "Z")
            self.store.save_transaction(
                signature=f"wallet-b-{index:04d}",
                source_wallet_address=second_wallet,
                classification="UNKNOWN",
                status="observed",
                slot=20000 + index,
                block_time=1700000600 + index,
                profile_ids=[profile],
                observed_at=observed,
            )

        with sqlite3.connect(self.path) as db:
            total_links = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=?",
                (profile,),
            ).fetchone()[0]
            wallet_a_links = db.execute(
                """SELECT COUNT(*) FROM profile_transactions p
                   JOIN transactions t ON t.signature=p.signature
                   WHERE p.profile_id=? AND t.source_wallet_address=?""",
                (profile, PUBLIC_ADDRESS),
            ).fetchone()[0]
            wallet_b_links = db.execute(
                """SELECT COUNT(*) FROM profile_transactions p
                   JOIN transactions t ON t.signature=p.signature
                   WHERE p.profile_id=? AND t.source_wallet_address=?""",
                (profile, second_wallet),
            ).fetchone()[0]
            oldest_link = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=? AND signature='wallet-a-0000'",
                (profile,),
            ).fetchone()[0]
            latest_a_link = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=? AND signature='wallet-a-0599'",
                (profile,),
            ).fetchone()[0]
            earliest_b_link = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=? AND signature='wallet-b-0000'",
                (profile,),
            ).fetchone()[0]
            newest_b_link = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=? AND signature='wallet-b-0599'",
                (profile,),
            ).fetchone()[0]
            old_transaction_still_exists = db.execute(
                "SELECT COUNT(*) FROM transactions WHERE signature='wallet-a-0000'"
            ).fetchone()[0]

        self.assertEqual(total_links, 1000)
        self.assertEqual(wallet_a_links, 400)
        self.assertEqual(wallet_b_links, 600)
        self.assertEqual(oldest_link, 0)
        self.assertEqual(latest_a_link, 1)
        self.assertEqual(earliest_b_link, 1)
        self.assertEqual(newest_b_link, 1)
        self.assertEqual(old_transaction_still_exists, 0)
        self.assertEqual(len(self.store.list_transactions(profile)), 1000)

    def test_pruning_one_profile_does_not_unlink_shared_history_from_another(self):
        first = self.store.create_profile("First")
        second = self.store.create_profile("Second")
        self.store.associate_source_wallet(first, PUBLIC_ADDRESS)
        self.store.associate_source_wallet(second, PUBLIC_ADDRESS)
        self.store.save_transaction(
            "shared-old-signature", PUBLIC_ADDRESS,
            profile_ids=[first, second],
            observed_at="2026-01-01T00:00:00Z",
            block_time=1700000000,
        )

        start = datetime(2026, 1, 2, tzinfo=timezone.utc)
        for index in range(1000):
            observed = (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
            self.store.save_transaction(
                signature=f"first-only-{index:04d}",
                source_wallet_address=PUBLIC_ADDRESS,
                profile_ids=[first],
                observed_at=observed,
                block_time=1700001000 + index,
            )

        with sqlite3.connect(self.path) as db:
            first_link = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=? AND signature=?",
                (first, "shared-old-signature"),
            ).fetchone()[0]
            second_link = db.execute(
                "SELECT COUNT(*) FROM profile_transactions WHERE profile_id=? AND signature=?",
                (second, "shared-old-signature"),
            ).fetchone()[0]
            stored_transaction = db.execute(
                "SELECT COUNT(*) FROM transactions WHERE signature='shared-old-signature'"
            ).fetchone()[0]

        self.assertEqual(first_link, 0)
        self.assertEqual(second_link, 1)
        self.assertEqual(stored_transaction, 1)

    def test_balance_snapshots_keep_history_and_latest_current(self):
        p = self.store.create_profile("Portfolio")
        self.store.associate_source_wallet(p, PUBLIC_ADDRESS)
        self.store.save_balance_snapshot(p, PUBLIC_ADDRESS, PUBLIC_ADDRESS, "1000000", 6,
                                         source="public-rpc", observed_at="2026-01-01T00:00:00Z")
        self.store.save_balance_snapshot(p, PUBLIC_ADDRESS, PUBLIC_ADDRESS, "0", 6,
                                         source="public-rpc", observed_at="2026-01-02T00:00:00Z",
                                         freshness_seconds=5)
        self.assertEqual(self.store.current_balances(p)[0]["raw_amount"], "0")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM balance_snapshots WHERE is_current=1").fetchone()[0], 1)

    def test_soft_delete_preserves_history(self):
        p = self.store.create_profile("History")
        self.store.associate_source_wallet(p, PUBLIC_ADDRESS)
        self.store.save_transaction("sample-signature", PUBLIC_ADDRESS, profile_ids=[p])
        self.store.delete_profile(p)
        self.assertIsNone(self.store.get_profile(p))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_transactions").fetchone()[0], 1)

    def test_restart_persistence_and_schema_version(self):
        p = self.store.create_profile("Restart")
        restarted = ProfileStore(self.path)
        self.assertEqual(restarted.get_profile(p)["name"], "Restart")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_invalid_values_are_rejected(self):
        p = self.store.create_profile("Validation")
        self.store.associate_source_wallet(p, PUBLIC_ADDRESS)
        with self.assertRaises(ValueError):
            self.store.save_transaction("bad-label", PUBLIC_ADDRESS, classification="BUY")
        with self.assertRaises(ValueError):
            self.store.save_transaction("bad-decimals", PUBLIC_ADDRESS, token_decimals=31)
        with self.assertRaises(ValueError):
            self.store.save_balance_snapshot(p, PUBLIC_ADDRESS, PUBLIC_ADDRESS, "1.2.3", 6,
                                             source="public-rpc")


    def test_portfolio_aggregates_current_balances_without_float_rounding(self):
        p = self.store.create_profile("Aggregate")
        second_wallet = "So11111111111111111111111111111111111111112"
        self.store.associate_source_wallet(p, PUBLIC_ADDRESS)
        self.store.associate_source_wallet(p, second_wallet)
        self.store.save_balance_snapshot(
            p, PUBLIC_ADDRESS, PUBLIC_ADDRESS, "1000000", 6,
            source="rpc-a", observed_at="2026-10-25T00:00:00Z")
        self.store.save_balance_snapshot(
            p, second_wallet, PUBLIC_ADDRESS, "250000", 6,
            source="rpc-b", observed_at="2026-10-25T00:01:00Z")
        holding = self.store.portfolio_holdings(p)[0]
        self.assertEqual(holding["raw_amount"], "1250000")
        self.assertEqual(holding["amount"], "1.25")
        self.assertEqual(holding["wallet_count"], 2)
        self.assertIsNone(holding["metadata"])

    def test_zero_balance_is_retained_and_backup_can_be_restored(self):
        p = self.store.create_profile("Backup")
        self.store.associate_source_wallet(p, PUBLIC_ADDRESS)
        self.store.save_balance_snapshot(
            p, PUBLIC_ADDRESS, PUBLIC_ADDRESS, "0", 6,
            source="public-rpc", observed_at="2026-10-25T00:00:00Z")
        backup = Path(self.temp.name) / "backup.db"
        self.store.backup_to(backup)
        restored = ProfileStore(backup)
        self.assertEqual(restored.current_balances(p)[0]["raw_amount"], "0")
        self.assertEqual(restored.get_profile(p)["name"], "Backup")

    def test_invalid_public_address_and_timezone_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_profile("Invalid", destination_public_address="not-an-address")
        with self.assertRaises(ValueError):
            self.store.save_transaction(
                "bad-time", PUBLIC_ADDRESS, observed_at="2026-10-25T00:00:00")

if __name__ == "__main__":
    unittest.main()
