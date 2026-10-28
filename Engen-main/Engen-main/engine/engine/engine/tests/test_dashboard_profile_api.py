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
from engine import paper_copy_runner

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

    def test_wallet_selection_can_be_removed_without_orphaning_saved_wallets(self):
        wallets_path = Path(self.temp.name) / "wallets.json"
        wallets_path.write_text(json.dumps([PUBLIC_WALLET]), encoding="utf-8")

        with patch.object(dashboard_server, "WALLETS_FILE", wallets_path), \
             patch.object(dashboard_server, "DASHBOARD_AUTH_TOKEN", ""), \
             patch.object(dashboard_server.monitor_service, "status", return_value={"enabled": False}):
            http = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%s" % http.server_port

            def post_json(path, payload):
                request = Request(
                    base + path,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=3) as response:
                        return response.status, json.loads(response.read().decode("utf-8"))
                except HTTPError as error:
                    return error.code, json.loads(error.read().decode("utf-8"))

            try:
                status, body = post_json(
                    "/api/wallets", {"action": "remove", "address": PUBLIC_WALLET}
                )
                self.assertEqual(status, 409)
                self.assertIn("assigned", body["error"])
                self.assertEqual(json.loads(wallets_path.read_text(encoding="utf-8")), [PUBLIC_WALLET])

                status, body = post_json(
                    "/api/profiles",
                    {
                        "action": "disassociate-source-wallet",
                        "profile_id": self.profile_id,
                        "wallet_address": PUBLIC_WALLET,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["profile"]["source_wallets"], [])

                status, body = post_json(
                    "/api/profiles",
                    {"action": "update", "profile_id": self.profile_id, "status": "active"},
                )
                self.assertEqual(status, 400)
                self.assertIn("destination public address", body["error"])

                status, body = post_json(
                    "/api/profiles",
                    {
                        "action": "update",
                        "profile_id": self.profile_id,
                        "destination_public_address": TOKEN_MINT,
                    },
                )
                self.assertEqual(status, 200)

                status, body = post_json(
                    "/api/profiles",
                    {"action": "update", "profile_id": self.profile_id, "status": "active"},
                )
                self.assertEqual(status, 400)
                self.assertIn("source public wallet", body["error"])

                status, body = post_json(
                    "/api/wallets", {"action": "remove", "address": PUBLIC_WALLET}
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["wallets"], [])
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=3)

    def test_profile_name_can_be_updated_through_profile_api(self):
        with patch.object(dashboard_server, "DASHBOARD_AUTH_TOKEN", ""):
            http = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%s" % http.server_port
            request = Request(
                base + "/api/profiles",
                data=json.dumps({
                    "action": "update",
                    "profile_id": self.profile_id,
                    "name": "Renamed paper profile",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=3) as response:
                    status = response.status
                    body = json.loads(response.read().decode("utf-8"))
                self.assertEqual(status, 200)
                self.assertEqual(body["profile"]["name"], "Renamed paper profile")
                self.assertEqual(
                    self.store.get_profile(self.profile_id)["name"],
                    "Renamed paper profile",
                )
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=3)

    def test_api_can_adjust_virtual_usd_balance_with_audit_history(self):
        data_dir = Path(self.temp.name) / "profile-data"
        with patch.object(dashboard_server, "DASHBOARD_AUTH_TOKEN", ""), \
             patch.object(paper_copy_runner, "PROFILE_DATA_DIR", data_dir):
            http = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%s" % http.server_port

            def request_json(path, payload=None):
                data = None if payload is None else json.dumps(payload).encode("utf-8")
                headers = {} if payload is None else {"Content-Type": "application/json"}
                request = Request(
                    base + path, data=data, headers=headers,
                    method="GET" if payload is None else "POST",
                )
                try:
                    with urlopen(request, timeout=3) as response:
                        return response.status, json.loads(response.read().decode("utf-8"))
                except HTTPError as error:
                    return error.code, json.loads(error.read().decode("utf-8"))

            try:
                status, body = request_json("/api/paper-simulation", {
                    "action": "set-initial-balance", "profile_id": self.profile_id,
                    "mint": "USD", "amount": "1000",
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["result"]["balance"], "1000")
                self.assertFalse(body["result"]["real_trading"])
                self.assertFalse(body["result"]["transaction_broadcast"])

                status, body = request_json("/api/paper-simulation", {
                    "action": "set-balance", "profile_id": self.profile_id,
                    "mint": "USD", "amount": "625.50",
                })
                self.assertEqual(status, 200)
                self.assertEqual(body["result"]["adjustment"], "-374.50")
                self.assertEqual(body["result"]["balance"], "625.50")
                self.assertEqual(body["report"]["initial_balance"], "1000")
                self.assertEqual(body["report"]["balances"]["USD"], "625.50")
                self.assertEqual(body["report"]["transactions"][-1]["kind"], "BALANCE_ADJUSTMENT")
                self.assertEqual(body["report"]["transactions"][-1]["amount"], "-374.50")
                self.assertFalse(body["report"]["real_trading"])
                self.assertFalse(body["report"]["transaction_broadcast"])

                status, body = request_json("/api/paper-simulation", {
                    "action": "set-balance", "profile_id": self.profile_id,
                    "mint": "USD", "amount": "-1",
                })
                self.assertEqual(status, 400)
                self.assertIn("non-negative", body["error"])
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=3)

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

    def test_profile_paper_simulator_reports_balance_activity_without_cross_profile_leakage(self):
        second_profile = self.store.create_profile("Second paper simulator profile")
        first_data_dir = dashboard_server.PROFILE_DATA_DIR

        with patch.object(paper_copy_runner, "PROFILE_DATA_DIR", first_data_dir):
            with patch.dict(paper_copy_runner._PROFILE_SIMULATORS, {}, clear=True):
                first = paper_copy_runner.get_paper_simulator(self.profile_id)
                second = paper_copy_runner.get_paper_simulator(second_profile)

                first.set_initial_virtual_balance("1000")
                first.add_virtual_balance("USD", "250")
                second.set_initial_virtual_balance("40")

                first_report = dashboard_server.load_paper_simulation_report(self.profile_id)
                second_report = dashboard_server.load_paper_simulation_report(second_profile)

        self.assertEqual(first_report["initial_balance"], "1000")
        self.assertEqual(first_report["balances"]["USD"], "1250")
        self.assertEqual(first_report["available_balance_usd"], "1250")
        self.assertEqual(first_report["portfolio_value_usd"], "1250")
        self.assertEqual(first_report["realized_pnl_usd"], "0")
        self.assertEqual(first_report["realized_pnl_percent"], "0.00")
        self.assertEqual(
            [event["kind"] for event in first_report["transactions"]],
            ["DEPOSIT", "INITIAL_BALANCE"],
        )
        self.assertEqual(second_report["initial_balance"], "40")
        self.assertEqual(second_report["balances"]["USD"], "40")
        self.assertEqual(
            [event["kind"] for event in second_report["transactions"]],
            ["INITIAL_BALANCE"],
        )
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
