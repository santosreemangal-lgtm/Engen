import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs

from dashboard import server as dashboard_server
from engine.profile_store import ProfileStore
from engine.paper_trader import PaperTrader

PUBLIC_WALLET = "11111111111111111111111111111111"
TOKEN_MINT = "So11111111111111111111111111111111111111112"


class DashboardProfileApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = dashboard_server.PROFILE_STORE_DB
        self.original_profile_data_dir = dashboard_server.PROFILE_DATA_DIR
        self.db_path = Path(self.temp.name) / "profiles.db"
        dashboard_server.PROFILE_STORE_DB = self.db_path
        dashboard_server.PROFILE_DATA_DIR = Path(self.temp.name) / "profile-data"
        self.store = ProfileStore(self.db_path)
        self.profile_id = self.store.create_profile("API test profile", settings={"risk_mode": "paper"})
        self.store.associate_source_wallet(self.profile_id, PUBLIC_WALLET)

    def tearDown(self):
        dashboard_server.PROFILE_STORE_DB = self.original_db
        dashboard_server.PROFILE_DATA_DIR = self.original_profile_data_dir
        dashboard_server.profile_monitor_services.pop(self.profile_id, None)
        self.temp.cleanup()

    def _post_dashboard_api(self, path, payload):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        request = Request(
            "http://127.0.0.1:%s%s" % (httpd.server_port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                return error.code, json.loads(error.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_start_monitoring_requires_active_profile_and_source_wallets(self):
        status, body = self._post_dashboard_api(
            "/api/profile-monitor", {"action": "start", "profile_id": self.profile_id}
        )
        self.assertEqual(status, 409)
        self.assertIn("Activate this profile", body["error"])

        no_sources = self.store.create_profile("No source wallet")
        self.store.update_profile(no_sources, status="active")
        status, body = self._post_dashboard_api(
            "/api/profile-monitor", {"action": "start", "profile_id": no_sources}
        )
        self.assertEqual(status, 400)
        self.assertIn("at least one source", body["error"])

    def test_start_and_stop_monitoring_api_uses_selected_public_wallets(self):
        self.store.update_profile(self.profile_id, status="active")
        state_path = Path(self.temp.name) / "monitoring_state.json"

        class FakeMonitorService:
            def __init__(self):
                self.started_addresses = None
                self.stopped = False

            def start(self, addresses):
                self.started_addresses = list(addresses)
                return {"enabled": True, "interval_seconds": 30,
                        "wallets": [{"address": address, "state": "waiting"} for address in addresses]}

            def stop(self):
                self.stopped = True
                return {"enabled": False, "interval_seconds": 30, "wallets": []}

        fake = FakeMonitorService()
        with patch.object(dashboard_server, "get_profile_monitor_service", return_value=fake), \\
             patch.object(dashboard_server, "MONITOR_STATE_FILE", state_path):
            status, body = self._post_dashboard_api(
                "/api/profile-monitor", {"action": "start", "profile_id": self.profile_id}
            )
            self.assertEqual(status, 200)
            self.assertEqual(fake.started_addresses, [PUBLIC_WALLET])
            self.assertTrue(body["monitor"]["enabled"])
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["profile_ids"], [self.profile_id])

            status, body = self._post_dashboard_api(
                "/api/profile-monitor", {"action": "stop", "profile_id": self.profile_id}
            )
            self.assertEqual(status, 200)
            self.assertTrue(fake.stopped)
            self.assertFalse(body["monitor"]["enabled"])
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["profile_ids"], [])
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
        self.assertFalse(report["transaction_broadcast"])

    def test_virtual_usd_balance_can_be_changed_and_survives_reload(self):
        first = dashboard_server.set_profile_virtual_balance(self.profile_id, "1250.50")
        self.assertEqual(first["mint"], "USD")
        self.assertEqual(first["amount"], "1250.50")
        self.assertTrue(first["balance_updated"])
        self.assertFalse(first["real_trading"])
        self.assertFalse(first["transaction_broadcast"])

        # A new simulator object models a dashboard/server refresh.
        dashboard_server.profile_paper_simulators.pop(self.profile_id, None)
        first_report = dashboard_server.load_profile_paper_simulation_report(self.profile_id)
        self.assertEqual(first_report["available_balance_usd"], "1250.50")
        self.assertEqual(first_report["portfolio_value_usd"], "1250.50")
        self.assertEqual(first_report["realized_pnl_usd"], "0")
        self.assertEqual(first_report["unrealized_pnl_usd"], "0")

        second = dashboard_server.set_profile_virtual_balance(self.profile_id, "2500.75")
        self.assertEqual(second["amount"], "2500.75")
        dashboard_server.profile_paper_simulators.pop(self.profile_id, None)
        second_report = dashboard_server.load_profile_paper_simulation_report(self.profile_id)
        self.assertEqual(second_report["balances"]["USD"], "2500.75")
        self.assertEqual(second_report["available_balance"], "2500.75")
        self.assertEqual(second_report["portfolio_value_usd"], "2500.75")
        self.assertFalse(second_report["real_trading"])
        self.assertFalse(second_report["transaction_broadcast"])
    def test_profile_paper_reports_keep_balances_and_history_separate(self):
        second_profile = self.store.create_profile("Second API profile")
        first_db = dashboard_server.profile_paper_db_path(self.profile_id)
        second_db = dashboard_server.profile_paper_db_path(second_profile)

        first = PaperTrader({"USDC": 10}, db_path=first_db)
        first.process({
            "signature": "profile-a-signature",
            "available": True,
            "status": "success",
            "trade_signal": {
                "classification": "BUY_CANDIDATE",
                "asset_mints": ["TOKEN_A"],
                "quote_mints": ["USDC"],
            },
            "token_balance_changes": [
                {"mint": "TOKEN_A", "delta_amount": "500", "decimals": 2},
                {"mint": "USDC", "delta_amount": "-1000000", "decimals": 6},
            ],
        })
        PaperTrader({"USDC": 20}, db_path=second_db)

        first_report = dashboard_server.load_paper_simulation_report(self.profile_id)
        second_report = dashboard_server.load_paper_simulation_report(second_profile)

        self.assertEqual(first_report["balances"], {"TOKEN_A": "5", "USDC": "9"})
        self.assertEqual(len(first_report["orders"]), 1)
        self.assertEqual(second_report["balances"], {"USDC": "20"})
        self.assertEqual(second_report["orders"], [])
        self.assertFalse(first_report["real_trading"])
        self.assertFalse(first_report["transaction_broadcast"])

    def test_profile_paper_report_rejects_non_uuid_profile_id(self):
        with self.assertRaises(ValueError):
            dashboard_server.load_paper_simulation_report("../../paper.db")

    def test_monitor_services_are_independent_per_profile(self):
        second_profile = self.store.create_profile("Second monitor profile")
        with patch.dict(dashboard_server.profile_monitor_services, {}, clear=True):
            first_service = dashboard_server.get_profile_monitor_service(self.profile_id)
            second_service = dashboard_server.get_profile_monitor_service(second_profile)
            self.assertIsNot(first_service, second_service)
            self.assertEqual(first_service.status()["wallets"], [])
            self.assertEqual(second_service.status()["wallets"], [])

    def test_missing_profile_database_is_not_created_by_get_helpers(self):
        self.store = None
        self.db_path.unlink()
        report = dashboard_server.load_profiles_report()
        self.assertFalse(report["database_available"])
        self.assertEqual(report["profiles"], [])
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
