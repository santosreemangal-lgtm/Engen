import sqlite3
import tempfile
import unittest
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

    def test_secret_setting_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_profile("Unsafe", settings={"private_key": "x"})

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


if __name__ == "__main__":
    unittest.main()
