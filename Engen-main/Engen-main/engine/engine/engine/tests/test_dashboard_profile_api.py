import io
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
from datetime import datetime, timezone
from decimal import Decimal

from dashboard import server as dashboard_server
from dashboard import monitor_bridge
from engine.profile_store import ProfileStore
from engine.paper_trader import PaperTrader
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice

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
        with patch.object(dashboard_server, "get_profile_monitor_service", return_value=fake), patch.object(dashboard_server, "MONITOR_STATE_FILE", state_path), patch.dict(dashboard_server.profile_monitor_services, {self.profile_id: fake}):
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

    def test_profile_report_includes_all_associated_source_wallets(self):
        second_wallet = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
        self.store.associate_source_wallet(self.profile_id, second_wallet)

        report = dashboard_server.load_profiles_report()

        self.assertTrue(report["database_available"])
        profile = next(item for item in report["profiles"] if item["profile_id"] == self.profile_id)
        self.assertEqual(set(profile["source_wallets"]), {PUBLIC_WALLET, second_wallet})

    def test_public_token_metadata_returns_name_and_symbol_from_pair(self):
        dashboard_server._TOKEN_METADATA_CACHE.pop(TOKEN_MINT, None)
        pair_payload = [{
            "chainId": "solana",
            "baseToken": {
                "address": TOKEN_MINT,
                "name": "Wrapped Solana",
                "symbol": "SOL",
            },
            "quoteToken": {
                "address": PUBLIC_WALLET,
                "name": "Test Quote",
                "symbol": "TST",
            },
            "liquidity": {"usd": 25000},
            "info": {"imageUrl": "https://dd.dexscreener.com/ds-data/tokens/solana/" + TOKEN_MINT + ".png"},
        }]
        with patch.object(
            dashboard_server,
            "urlopen",
            return_value=io.BytesIO(json.dumps(pair_payload).encode("utf-8")),
        ) as mocked_urlopen:
            report = dashboard_server.load_public_token_metadata([TOKEN_MINT])

        metadata = report["tokens"][TOKEN_MINT]
        self.assertTrue(metadata["found"])
        self.assertEqual(metadata["symbol"], "SOL")
        self.assertEqual(metadata["name"], "Wrapped Solana")
        self.assertEqual(
            metadata["icon_url"],
            "https://dd.dexscreener.com/ds-data/tokens/solana/" + TOKEN_MINT + ".png",
        )
        self.assertEqual(metadata["status"], "ok")
        mocked_urlopen.assert_called_once()

    def test_public_token_metadata_ignores_untrusted_image_hosts(self):
        dashboard_server._TOKEN_METADATA_CACHE.pop(TOKEN_MINT, None)
        pair_payload = [{
            "chainId": "solana",
            "baseToken": {
                "address": TOKEN_MINT,
                "name": "Wrapped Solana",
                "symbol": "SOL",
            },
            "liquidity": {"usd": 25000},
            "info": {"imageUrl": "https://example-attacker.invalid/token.png"},
        }]
        with patch.object(
            dashboard_server,
            "urlopen",
            return_value=io.BytesIO(json.dumps(pair_payload).encode("utf-8")),
        ):
            report = dashboard_server.load_public_token_metadata([TOKEN_MINT])

        metadata = report["tokens"][TOKEN_MINT]
        self.assertTrue(metadata["found"])
        self.assertIsNone(metadata["icon_url"])

    def test_safe_token_image_url_requires_https_and_approved_host(self):
        self.assertEqual(
            dashboard_server._safe_public_token_image_url(
                "https://dd.dexscreener.com/ds-data/tokens/solana/" + TOKEN_MINT + ".png"
            ),
            "https://dd.dexscreener.com/ds-data/tokens/solana/" + TOKEN_MINT + ".png",
        )
        self.assertIsNone(
            dashboard_server._safe_public_token_image_url("http://dd.dexscreener.com/token.png")
        )
        self.assertIsNone(
            dashboard_server._safe_public_token_image_url("https://example.com/token.png")
        )
        self.assertIsNone(
            dashboard_server._safe_public_token_image_url("https://dd.dexscreener.com.evil.test/token.png")
        )

    def test_public_token_metadata_rejects_invalid_mint_addresses(self):
        with self.assertRaises(ValueError):
            dashboard_server.load_public_token_metadata(["not-a-valid-mint"])

    def test_paper_sell_all_requires_confirmation_and_sells_only_copied_open_positions(self):
        now = datetime.now(timezone.utc).isoformat()
        simulator = PaperSimulator({"USD": "100"})
        copied_buy = simulator.create_order(
            "copy-buy-for-sell-all", "BUY", TOKEN_MINT, "2", "USD",
            created_at=now,
            details={"origin": "AUTOMATIC_WALLET_COPY", "source_wallet": PUBLIC_WALLET},
        )
        buy_price = MarketPrice(
            TOKEN_MINT, Decimal("10"), "USD", "test-market", now,
            liquidity_usd=Decimal("50000"), liquidity_source="test-market",
            liquidity_observed_at=now,
        )
        simulator.execute_order(
            copied_buy["order_id"], buy_price, now=now,
            slippage_bps=0, fee_bps=0, network_cost=0,
            min_liquidity_usd=1000,
        )
        simulator.balances["UNRELATED_PAPER_ASSET"] = Decimal("7")

        sale_price = MarketPrice(
            TOKEN_MINT, Decimal("12"), "USD", "test-market", now,
            liquidity_usd=Decimal("50000"), liquidity_source="test-market",
            liquidity_observed_at=now,
        )
        with patch.object(dashboard_server, "get_profile_paper_simulator", return_value=simulator), patch.object(
            dashboard_server, "fetch_market_price", return_value=sale_price
        ) as market_provider:
            status, body = self._post_dashboard_api(
                "/api/paper-simulation",
                {"action": "sell-all-profile-positions", "profile_id": self.profile_id},
            )
            self.assertEqual(status, 400)
            self.assertEqual(simulator.balances[TOKEN_MINT], Decimal("2"))
            self.assertEqual(len(simulator.fills), 1)
            market_provider.assert_not_called()

            status, body = self._post_dashboard_api(
                "/api/paper-simulation",
                {"action": "sell-all-profile-positions", "profile_id": self.profile_id, "confirmed": True},
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["sold_positions"], 1)
        self.assertEqual(body["outcomes"][0]["status"], "FILLED")
        self.assertEqual(simulator.balances[TOKEN_MINT], Decimal("0"))
        self.assertEqual(simulator.balances["UNRELATED_PAPER_ASSET"], Decimal("7"))
        self.assertFalse(body["real_trading"])
        self.assertFalse(body["transaction_broadcast"])

    def test_paper_sell_all_skips_position_without_fresh_public_market_data(self):
        now = datetime.now(timezone.utc).isoformat()
        simulator = PaperSimulator({"USD": "100"})
        copied_buy = simulator.create_order(
            "copy-buy-no-price", "BUY", TOKEN_MINT, "2", "USD",
            created_at=now,
            details={"origin": "AUTOMATIC_WALLET_COPY", "source_wallet": PUBLIC_WALLET},
        )
        buy_price = MarketPrice(
            TOKEN_MINT, Decimal("10"), "USD", "test-market", now,
            liquidity_usd=Decimal("50000"), liquidity_source="test-market",
            liquidity_observed_at=now,
        )
        simulator.execute_order(copied_buy["order_id"], buy_price, now=now,
                                 slippage_bps=0, fee_bps=0, network_cost=0)
        with patch.object(dashboard_server, "get_profile_paper_simulator", return_value=simulator), patch.object(
            dashboard_server, "fetch_market_price", return_value=None
        ):
            status, body = self._post_dashboard_api(
                "/api/paper-simulation",
                {"action": "sell-all-profile-positions", "profile_id": self.profile_id, "confirmed": True},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["sold_positions"], 0)
        self.assertEqual(body["skipped"][0]["status"], "SKIPPED")
        self.assertEqual(simulator.balances[TOKEN_MINT], Decimal("2"))
        self.assertEqual(len(simulator.fills), 1)

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
    def test_transaction_history_upgrades_pending_record_when_metadata_arrives(self):
        signature = "pending-then-confirmed"
        self.store.save_transaction(
            signature, PUBLIC_WALLET, classification="UNKNOWN", status="pending",
            evidence={"metadata_status": "pending"}, profile_ids=[self.profile_id],
        )
        self.store.save_transaction(
            signature, PUBLIC_WALLET, classification="BUY_CANDIDATE", status="confirmed",
            evidence={"metadata_status": "complete"}, slot=123, block_time=1700000000,
            raw_amount="1250000", token_mint=TOKEN_MINT, token_decimals=6,
            profile_ids=[self.profile_id],
        )
        # A stale retry must not downgrade an already terminal confirmed status.
        self.store.save_transaction(
            signature, PUBLIC_WALLET, classification="UNKNOWN", status="pending",
            evidence={}, profile_ids=[self.profile_id],
        )
        report = dashboard_server.load_profile_transactions(
            parse_qs("profile_id=" + self.profile_id + "&page=1&page_size=25")
        )
        self.assertEqual(report["total"], 1)
        item = report["transactions"][0]
        self.assertEqual(item["status"], "confirmed")
        self.assertEqual(item["classification"], "BUY_CANDIDATE")
        self.assertEqual(item["amount"], "1.25")
        self.assertEqual(item["evidence"]["metadata_status"], "complete")
    def test_monitor_bridge_persists_observed_transaction_metadata_separately(self):
        signature = "1" * 64
        observation = {
            "signature": signature,
            "available": True,
            "status": "success",
            "metadata_complete": True,
            "metadata_status": "available",
            "signers": [PUBLIC_WALLET],
            "signature_observation": {
                "confirmation_status": "confirmed", "slot": 123, "block_time": 1700000000,
            },
            "trade_signal": {
                "classification": "UNKNOWN", "reason": "test observation",
                "asset_mints": [TOKEN_MINT], "quote_mints": [],
            },
            "token_balance_changes": [
                {"owner": PUBLIC_WALLET, "mint": TOKEN_MINT,
                 "decimals": 6, "delta_amount": "1250000"},
            ],
        }

        class FakeSignatureStore:
            def mark_processed(self, wallet_address, stored_signature):
                self.last_processed = (wallet_address, stored_signature)

        with patch.object(monitor_bridge, "SignatureStore", FakeSignatureStore), patch.object(
            monitor_bridge, "monitor_wallet", return_value=[observation]
        ):
            scan = monitor_bridge.create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: dashboard_server.get_profile_paper_simulator(self.profile_id),
                profile_id=self.profile_id,
                profile_store=self.store,
            )
            results = scan(PUBLIC_WALLET)

        self.assertEqual(results[0]["profile_history_status"], "saved")
        report = dashboard_server.load_profile_transactions(
            parse_qs("profile_id=" + self.profile_id + "&page=1&page_size=25")
        )
        self.assertEqual(report["total"], 1)
        stored = report["transactions"][0]
        self.assertEqual(stored["signature"], signature)
        # UNKNOWN observations may retain transaction metadata, but must not
        # be assigned an arbitrary token mint or amount in the trade history.
        self.assertIsNone(stored["token_mint"])
        self.assertIsNone(stored["token_decimals"])
        self.assertIsNone(stored["amount"])
        self.assertEqual(
            stored["evidence"]["token_amount_attribution"],
            "unavailable_not_verified_swap",
        )
        self.assertFalse(stored["evidence"]["verified_swap_for_paper_queue"])
        self.assertFalse(stored["simulated"])
        paper_report = dashboard_server.load_profile_paper_simulation_report(self.profile_id)
        self.assertEqual(paper_report["orders"], [])
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
