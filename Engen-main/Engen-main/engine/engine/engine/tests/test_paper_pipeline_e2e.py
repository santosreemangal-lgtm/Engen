"""Isolated end-to-end verification for paper execution, monitoring speed, and history.

All chain observations and market prices are deterministic synthetic fixtures.
The test uses temporary SQLite databases only. It never accesses an external RPC,
wallet secret, transaction signer, live order endpoint, or user data.
"""
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard.monitor_bridge import create_scan_function
from dashboard.monitor_service import MonitorService
from engine.latency import LatencyRecorder
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice
from engine.profile_store import ProfileStore


WALLET = "11111111111111111111111111111111"
SECOND_WALLET = "So11111111111111111111111111111111111111112"
ASSET_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BUY_SIGNATURE = "1" * 63 + "2"
SELL_SIGNATURE = "1" * 63 + "3"


def confirmed_observation(signature, side):
    """Return a synthetic confirmed observation with consistent balance deltas."""
    if side == "BUY":
        asset_change = {
            "owner": WALLET, "mint": ASSET_MINT, "decimals": 6,
            "delta_amount": "1000000", "pre_amount": "0",
            "post_amount": "1000000", "source": "token",
        }
        quote_change = {
            "owner": WALLET, "mint": QUOTE_MINT, "decimals": 6,
            "delta_amount": "-5000000", "pre_amount": "10000000",
            "post_amount": "5000000", "source": "token",
        }
        classification = "BUY_CANDIDATE"
        slot = 100
    else:
        asset_change = {
            "owner": WALLET, "mint": ASSET_MINT, "decimals": 6,
            "delta_amount": "-500000", "pre_amount": "1000000",
            "post_amount": "500000", "source": "token",
        }
        quote_change = {
            "owner": WALLET, "mint": QUOTE_MINT, "decimals": 6,
            "delta_amount": "2500000", "pre_amount": "5000000",
            "post_amount": "7500000", "source": "token",
        }
        classification = "SELL_CANDIDATE"
        slot = 101

    return {
        "signature": signature,
        "status": "success",
        "metadata_complete": True,
        "metadata_status": "available",
        "retryable": False,
        "signers": [WALLET],
        "signature_observation": {
            "confirmation_status": "confirmed",
            "slot": slot,
            "block_time": 1700000000 + slot,
        },
        "trade_signal": {
            "classification": classification,
            "reason": "Deterministic E2E fixture.",
            "activity_type": classification,
            "asset_mints": [ASSET_MINT],
            "quote_mints": [QUOTE_MINT],
        },
        "token_balance_changes": [asset_change, quote_change],
    }


def fresh_market_price(mint, price):
    now = datetime.now(timezone.utc).isoformat()
    return MarketPrice(
        mint=mint,
        price=Decimal(price),
        currency="USD",
        source="synthetic-e2e-fixture",
        observed_at=now,
        liquidity_usd=Decimal("50000"),
        liquidity_source="synthetic-e2e-fixture",
        liquidity_observed_at=now,
    )


class PaperPipelineEndToEndTests(unittest.TestCase):
    def test_buy_sell_fills_history_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_db = root / "profiles.db"
            profile_data = root / "profile-data"
            profile_store = ProfileStore(profile_db)
            profile_id = profile_store.create_profile("Synthetic E2E")
            profile_store.associate_source_wallet(profile_id, WALLET)

            paper_db = profile_data / profile_id / "paper_trading.db"
            paper_db.parent.mkdir(parents=True)
            simulator = PaperSimulator(db_path=paper_db)
            simulator.set_initial_virtual_balance("1000", "USD")

            signature_store = Mock()
            latency = LatencyRecorder()
            prices = iter(("2", "3"))

            def synthetic_price_provider(mint):
                return fresh_market_price(mint, next(prices))

            simulator_lock = threading.RLock()
            scan_options = dict(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {
                    "sizing_mode": "FIXED_USD",
                    "sizing_value": "10",
                    "max_buy_usd": "10",
                },
                simulator_getter=lambda: simulator,
                simulator_lock_getter=lambda: simulator_lock,
                profile_id=profile_id,
                profile_store=profile_store,
                latency_recorder=latency,
                price_provider=synthetic_price_provider,
            )

            observations = [
                [confirmed_observation(BUY_SIGNATURE, "BUY")],
                [confirmed_observation(SELL_SIGNATURE, "SELL")],
            ]
            with (
                patch("dashboard.monitor_bridge.SignatureStore", return_value=signature_store),
                patch("dashboard.monitor_bridge.monitor_wallet", side_effect=observations),
            ):
                # Construct the bridge only after patching its persistent store,
                # so this test never creates or modifies the repository data/ DB.
                scan = create_scan_function(
                    **scan_options
                )
                buy_result = scan(WALLET)
                sell_result = scan(WALLET)

            # Both observations must pass the real bridge/queue/simulator path.
            self.assertEqual(len(buy_result), 1)
            self.assertEqual(len(sell_result), 1)
            self.assertEqual(
                buy_result[0]["paper_copy_order"]["execution_status"], "FILLED"
            )
            self.assertEqual(
                sell_result[0]["paper_copy_order"]["execution_status"], "FILLED"
            )
            self.assertEqual(
                buy_result[0]["paper_copy_order"]["side"], "BUY"
            )
            self.assertEqual(
                sell_result[0]["paper_copy_order"]["side"], "SELL"
            )
            self.assertEqual(buy_result[0]["profile_history_status"], "saved")
            self.assertEqual(sell_result[0]["profile_history_status"], "saved")
            self.assertEqual(signature_store.mark_processed.call_count, 2)

            # Validate the live in-test ledger before forcing a SQLite restart.
            self.assertEqual(len(simulator.orders), 2)
            self.assertEqual({o["status"] for o in simulator.orders.values()}, {"FILLED"})
            self.assertEqual(len(simulator.fills), 2)
            self.assertEqual(simulator.balances[ASSET_MINT], Decimal("2.5"))
            self.assertGreater(simulator.balances["USD"], Decimal("0"))
            self.assertTrue(all(
                order["details"]["real_trading"] is False
                and order["details"]["transaction_broadcast"] is False
                for order in simulator.orders.values()
            ))

            # A brand-new simulator instance must recover fills, orders, balances,
            # and the realized P&L ledger from the temporary SQLite database.
            recovered = PaperSimulator(db_path=paper_db)
            self.assertEqual(len(recovered.orders), 2)
            self.assertEqual(len(recovered.fills), 2)
            self.assertEqual({o["status"] for o in recovered.orders.values()}, {"FILLED"})
            self.assertEqual(recovered.balances[ASSET_MINT], Decimal("2.5"))
            self.assertEqual(recovered.balances["USD"], simulator.balances["USD"])
            realized = recovered.pnl_summary()["realized_by_mint"][ASSET_MINT]
            self.assertEqual(realized["status"], "calculated")
            self.assertGreater(Decimal(realized["pnl"]), Decimal("0"))

            # The bridge must expose latency for the complete monitor-to-fill path.
            metrics = latency.summary()
            for stage in (
                "dashboard.wallet_scan_total",
                "dashboard.wallet_monitor_and_transaction_processing",
                "dashboard.transaction_history_persist",
                "dashboard.paper_order_queue",
                "dashboard.paper_market_data_fetch",
                "dashboard.paper_simulation",
                "dashboard.paper_result_commit",
                "dashboard.paper_lock_wait",
            ):
                self.assertIn(stage, metrics)

            # Read both dashboard history surfaces after clearing the simulator
            # cache so the paper report must reopen the persisted SQLite database.
            with (
                patch("dashboard.server.PROFILE_STORE_DB", profile_db),
                patch("dashboard.server.PROFILE_DATA_DIR", profile_data),
                patch.dict("dashboard.server.profile_paper_simulators", {}, clear=True),
                patch("dashboard.server.fetch_market_price", return_value=None),
            ):
                from dashboard.server import (
                    load_profile_paper_simulation_report,
                    load_profile_transactions,
                )

                transaction_history = load_profile_transactions({
                    "profile_id": [profile_id],
                    "page": ["1"],
                    "page_size": ["25"],
                })
                paper_history = load_profile_paper_simulation_report(profile_id)

            self.assertTrue(transaction_history["database_available"])
            self.assertEqual(transaction_history["total"], 2)
            self.assertEqual(
                {item["signature"] for item in transaction_history["transactions"]},
                {BUY_SIGNATURE, SELL_SIGNATURE},
            )
            self.assertEqual(
                {item["classification"] for item in transaction_history["transactions"]},
                {"BUY_CANDIDATE", "SELL_CANDIDATE"},
            )
            self.assertTrue(paper_history["database_available"])
            self.assertEqual(len(paper_history["orders"]), 2)
            self.assertEqual(len(paper_history["fills"]), 2)
            self.assertEqual({item["status"] for item in paper_history["orders"]}, {"FILLED"})
            self.assertEqual(
                Decimal(paper_history["available_balance"]),
                recovered.balances["USD"],
            )
            self.assertGreater(Decimal(paper_history["realized_pnl_usd"]), Decimal("0"))
            self.assertEqual(paper_history["realized_sell_fill_count"], 1)
            self.assertEqual(paper_history["realized_pnl_status"], "calculated")
            self.assertFalse(paper_history["real_trading"])
            self.assertFalse(paper_history["transaction_broadcast"])

    def test_buy_only_session_does_not_report_realized_profit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_db = root / "profiles.db"
            profile_data = root / "profile-data"
            profile_store = ProfileStore(profile_db)
            profile_id = profile_store.create_profile("Synthetic BUY-only")

            paper_db = profile_data / profile_id / "paper_trading.db"
            paper_db.parent.mkdir(parents=True)
            simulator = PaperSimulator(db_path=paper_db)
            simulator.set_initial_virtual_balance("1000", "USD")

            price = fresh_market_price("TOKEN_A", "2")
            order = simulator.create_order(
                signal_id="buy-only-signal",
                side="BUY",
                asset_mint="TOKEN_A",
                quantity="1",
                quote_mint="USD",
                created_at=price.observed_at,
                details={"target_notional_usd": "10"},
            )
            result = simulator.execute_order(
                order["order_id"],
                price,
                now=price.observed_at,
            )
            self.assertEqual(result["status"], "FILLED")
            self.assertEqual(len(simulator.fills), 1)
            self.assertEqual(simulator.fills[0]["side"], "BUY")

            with (
                patch("dashboard.server.PROFILE_STORE_DB", profile_db),
                patch("dashboard.server.PROFILE_DATA_DIR", profile_data),
                patch.dict("dashboard.server.profile_paper_simulators", {}, clear=True),
                patch("dashboard.server.fetch_market_price", return_value=None),
            ):
                from dashboard.server import load_profile_paper_simulation_report

                report = load_profile_paper_simulation_report(profile_id)

            self.assertTrue(report["database_available"])
            self.assertEqual(report["initial_balance"], "1000")
            self.assertEqual(report["realized_sell_fill_count"], 0)
            self.assertEqual(report["realized_pnl_status"], "no_sell_fills")
            self.assertEqual(report["realized_pnl_usd"], "0")
            self.assertEqual(len(report["fills"]), 1)
            self.assertIsNone(report["unrealized_pnl_usd"])
            self.assertFalse(report["real_trading"])
            self.assertFalse(report["transaction_broadcast"])

    def test_monitor_scans_wallets_concurrently_and_reports_cycle_timing(self):
        rendezvous = threading.Barrier(2)

        def concurrent_scan(_address):
            # A serial worker configuration times out here; two configured
            # workers must overlap their first scans.
            rendezvous.wait(timeout=3)
            return []

        service = MonitorService(concurrent_scan, interval=15, max_workers=2)
        try:
            service.start([WALLET, SECOND_WALLET])
            deadline = time.monotonic() + 5
            status = service.status()
            while time.monotonic() < deadline:
                status = service.status()
                if (
                    status.get("last_cycle_duration_ms") is not None
                    and all(wallet.get("scan_count", 0) >= 1 for wallet in status["wallets"])
                ):
                    break
                time.sleep(0.01)

            self.assertEqual(status["scan_workers"], 2)
            self.assertEqual(status["last_cycle_wallet_count"], 2)
            self.assertIsNotNone(status["last_cycle_started_at"])
            self.assertIsNotNone(status["last_cycle_completed_at"])
            self.assertGreaterEqual(status["last_cycle_duration_ms"], 0)
            self.assertEqual(
                {wallet["state"] for wallet in status["wallets"]},
                {"connected"},
            )
            self.assertTrue(all(wallet["scan_duration_ms"] is not None for wallet in status["wallets"]))
        finally:
            service.stop_and_wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
