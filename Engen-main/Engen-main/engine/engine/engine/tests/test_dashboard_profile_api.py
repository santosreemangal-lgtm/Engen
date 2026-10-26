import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs

from dashboard import server as dashboard_server
from engine.profile_store import ProfileStore

PUBLIC_WALLET = "11111111111111111111111111111111"
TOKEN_MINT = "So11111111111111111111111111111111111111112"


class DashboardProfileApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = dashboard_server.PROFILE_STORE_DB
        self.db_path = Path(self.temp.name) / "profiles.db"
        dashboard_server.PROFILE_STORE_DB = self.db_path
        self.store = ProfileStore(self.db_path)
        self.profile_id = self.store.create_profile("API test profile", settings={"risk_mode": "paper"})
        self.store.associate_source_wallet(self.profile_id, PUBLIC_WALLET)

    def tearDown(self):
        dashboard_server.PROFILE_STORE_DB = self.original_db
        self.temp.cleanup()

    def test_profile_report_includes_provenance_and_marks_observed_not_simulated(self):
        self.store.save_balance_snapshot(
            self.profile_id, PUBLIC_WALLET, TOKEN_MINT, "1250000", 6,
            source="public-rpc", observed_at="2026-10-25T00:00:00Z",
            freshness_seconds=60,
        )
        report = dashboard_server.load_profiles_report()
        self.assertTrue(report["database_available"])
        self.assertEqual(len(report["profiles"]), 1)
        profile = report["profiles"][0]
        self.assertEqual(profile["profile_id"], self.profile_id)
        self.assertEqual(profile["source_wallets"], [PUBLIC_WALLET])
        self.assertEqual(profile["balances"][0]["amount"], "1.25")
        self.assertEqual(profile["balances"][0]["value_kind"], "OBSERVED_ON_CHAIN_SNAPSHOT")
        self.assertFalse(profile["balances"][0]["simulated"])
        self.assertEqual(profile["balances"][0]["source"], "public-rpc")

    def test_transaction_history_is_bounded_and_paginated(self):
        for index in range(3):
            self.store.save_transaction(
                "signature-" + str(index), PUBLIC_WALLET,
                classification="BUY_CANDIDATE" if index < 2 else "UNKNOWN",
                status="confirmed",
                evidence={"reason": "test metadata"},
                raw_amount=str((index + 1) * 1000000),
                token_mint=TOKEN_MINT,
                token_decimals=6,
                profile_ids=[self.profile_id],
                observed_at=f"2026-10-25T00:00:0{index}Z",
            )
        query = parse_qs("profile_id=" + self.profile_id + "&page=1&page_size=2")
        report = dashboard_server.load_profile_transactions(query)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["page"], 1)
        self.assertEqual(report["page_size"], 2)
        self.assertEqual(report["pages"], 2)
        self.assertEqual(len(report["transactions"]), 2)
        self.assertFalse(report["transactions"][0]["simulated"])

    def test_classification_filter_is_allowlisted_and_applied(self):
        self.store.save_transaction(
            "buy-signature", PUBLIC_WALLET, classification="BUY_CANDIDATE",
            status="confirmed", token_mint=TOKEN_MINT, token_decimals=6,
            profile_ids=[self.profile_id], observed_at="2026-10-25T00:00:00Z",
        )
        self.store.save_transaction(
            "unknown-signature", PUBLIC_WALLET, classification="UNKNOWN",
            status="unknown", token_mint=TOKEN_MINT, token_decimals=6,
            profile_ids=[self.profile_id], observed_at="2026-10-25T00:00:01Z",
        )
        query = parse_qs("profile_id=" + self.profile_id + "&classification=UNKNOWN")
        report = dashboard_server.load_profile_transactions(query)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["transactions"][0]["classification"], "UNKNOWN")

    def test_invalid_pagination_and_filter_values_are_rejected(self):
        with self.assertRaises(ValueError):
            dashboard_server.load_profile_transactions(
                parse_qs("profile_id=" + self.profile_id + "&page_size=1000")
            )
        with self.assertRaises(ValueError):
            dashboard_server.load_profile_transactions(
                parse_qs("profile_id=" + self.profile_id + "&classification=BUY")
            )


    def test_direct_rpc_health_probe_reports_read_only_success(self):
        with patch.object(dashboard_server, "get_public_health", return_value="ok"):
            report = dashboard_server.rpc_health_report()
        self.assertEqual(report["mode"], "READ_ONLY")
        self.assertTrue(report["available"])
        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["transaction_broadcast"])
        self.assertIn("checked_at", report)

    def test_direct_rpc_health_probe_reports_provider_failure_without_crashing(self):
        with patch.object(
            dashboard_server, "get_public_health",
            side_effect=dashboard_server.SolanaRPCError("provider unavailable"),
        ):
            report = dashboard_server.rpc_health_report()
        self.assertFalse(report["available"])
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["error"], "SolanaRPCError")
        self.assertFalse(report["transaction_broadcast")

    def test_missing_profile_database_is_not_created_by_get_helpers(self):
        self.store = None
        self.db_path.unlink()
        report = dashboard_server.load_profiles_report()
        self.assertFalse(report["database_available"])
        self.assertEqual(report["profiles"], [])
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
