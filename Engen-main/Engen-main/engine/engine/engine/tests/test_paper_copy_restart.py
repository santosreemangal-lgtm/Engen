"""Restart-recovery tests for the automatic paper-copy path.

These tests prove that queued/filled paper state survives a new process-local
PaperSimulator instance loading the same SQLite file. No real RPC, signing,
or broadcast is involved.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from engine.latency import LatencyRecorder
from engine.paper_copy_runner import (
    execute_pending_paper_orders,
    get_pending_copy_status,
    queue_paper_orders,
    reset_paper_simulator_for_tests,
)
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def candidate_result(*, signature="sig-restart-1", classification="BUY_CANDIDATE"):
    asset_delta = "2500000" if classification == "BUY_CANDIDATE" else "-2500000"
    quote_delta = "-10000000" if classification == "BUY_CANDIDATE" else "10000000"
    asset_pre = "0" if classification == "BUY_CANDIDATE" else "10000000"
    asset_post = "2500000" if classification == "BUY_CANDIDATE" else "7500000"
    return {
        "signature": signature,
        "status": "success",
        "metadata_complete": True,
        "signers": [WALLET],
        "signature_observation": {
            "confirmation_status": "finalized",
            "slot": 123,
            "block_time": 1700000000,
        },
        "trade_signal": {
            "classification": classification,
            "asset_mints": [ASSET],
            "quote_mints": [QUOTE],
        },
        "token_balance_changes": [
            {
                "owner": WALLET,
                "mint": ASSET,
                "decimals": 6,
                "delta_amount": asset_delta,
                "pre_amount": asset_pre,
                "post_amount": asset_post,
            },
            {
                "owner": WALLET,
                "mint": QUOTE,
                "decimals": 6,
                "delta_amount": quote_delta,
            },
        ],
    }


def fresh_price_provider(price="4", liquidity="20000"):
    observed_at = datetime.now(timezone.utc).isoformat()

    def provider(mint):
        return MarketPrice(
            mint=mint,
            price=Decimal(price),
            currency="USD",
            source="test-provider",
            observed_at=observed_at,
            liquidity_usd=Decimal(liquidity),
            liquidity_source="test-provider",
            liquidity_observed_at=observed_at,
        )

    return provider


class PaperCopyRestartTests(unittest.TestCase):
    def setUp(self):
        reset_paper_simulator_for_tests()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "paper_simulator.db"

    def tearDown(self):
        reset_paper_simulator_for_tests()
        self._tmpdir.cleanup()

    def test_filled_copy_survives_new_simulator_instance(self):
        """Simulate process restart by loading the same SQLite file in a new instance."""
        first = PaperSimulator(db_path=self.db_path)
        first.add_virtual_balance("USD", "1000")

        queued = queue_paper_orders(
            WALLET,
            [candidate_result()],
            first,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "20", "max_buy_usd": 100},
        )
        self.assertEqual(len(queued), 1)

        outcomes = execute_pending_paper_orders(
            WALLET, first, fresh_price_provider()
        )
        self.assertEqual(outcomes[0]["status"], "FILLED")
        self.assertIn("elapsed_ms", outcomes[0])

        report_before = first.report()
        self.assertEqual(report_before["orders"][0]["status"], "FILLED")
        filled_qty = Decimal(report_before["fills"][0]["quantity"])
        usd_after = Decimal(report_before["balances"]["USD"])
        asset_after = Decimal(report_before["balances"][ASSET])
        self.assertEqual(filled_qty, Decimal("5"))  # $20 / $4
        self.assertLess(usd_after, Decimal("1000"))
        self.assertEqual(asset_after, Decimal("5"))

        # New process-local instance, same database file.
        second = PaperSimulator(db_path=self.db_path)
        report_after = second.report()

        self.assertEqual(len(report_after["orders"]), 1)
        self.assertEqual(report_after["orders"][0]["status"], "FILLED")
        self.assertEqual(len(report_after["fills"]), 1)
        self.assertEqual(Decimal(report_after["fills"][0]["quantity"]), filled_qty)
        self.assertEqual(Decimal(report_after["balances"]["USD"]), usd_after)
        self.assertEqual(Decimal(report_after["balances"][ASSET]), asset_after)
        self.assertFalse(report_after["real_trading"])
        self.assertFalse(report_after["transaction_broadcast"])

        # Duplicate signal must not create a second order after restart.
        again = queue_paper_orders(
            WALLET,
            [candidate_result()],
            second,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "20", "max_buy_usd": 100},
        )
        self.assertEqual(again, [])
        self.assertEqual(len(second.report()["orders"]), 1)

    def test_pending_order_survives_restart_and_can_fill_later(self):
        first = PaperSimulator(db_path=self.db_path)
        first.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(WALLET, [candidate_result(signature="sig-pending-1")], first)
        self.assertEqual(len(queued), 1)

        # No market data yet — order stays CREATED.
        outcomes = execute_pending_paper_orders(
            WALLET, first, price_provider=lambda mint: None
        )
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(first.report()["orders"][0]["status"], "CREATED")

        pending = get_pending_copy_status(simulator=first, wallet_address=WALLET)
        self.assertEqual(pending["pending_count"], 1)
        self.assertEqual(pending["pending_orders"][0]["source_signature"], "sig-pending-1")

        # Restart: new instance loads the same pending order.
        second = PaperSimulator(db_path=self.db_path)
        pending_after = get_pending_copy_status(simulator=second, wallet_address=WALLET)
        self.assertEqual(pending_after["pending_count"], 1)
        self.assertEqual(second.report()["orders"][0]["status"], "CREATED")

        # Later scan with market data fills it.
        fill_outcomes = execute_pending_paper_orders(
            WALLET, second, fresh_price_provider()
        )
        self.assertEqual(fill_outcomes[0]["status"], "FILLED")
        self.assertEqual(second.report()["orders"][0]["status"], "FILLED")
        self.assertEqual(get_pending_copy_status(simulator=second, wallet_address=WALLET)["pending_count"], 0)

    def test_latency_recorder_captures_copy_stages(self):
        simulator = PaperSimulator(db_path=self.db_path)
        simulator.add_virtual_balance("USD", "1000")
        recorder = LatencyRecorder()

        queue_paper_orders(
            WALLET,
            [candidate_result(signature="sig-latency-1")],
            simulator,
            latency_recorder=recorder,
        )
        outcomes = execute_pending_paper_orders(
            WALLET,
            simulator,
            fresh_price_provider(),
            latency_recorder=recorder,
        )
        self.assertEqual(outcomes[0]["status"], "FILLED")

        summary = recorder.summary()
        self.assertIn("paper.copy_queue_batch", summary)
        self.assertIn("paper.copy_price_lookup", summary)
        self.assertIn("paper.copy_execute_order", summary)
        self.assertIn("paper.copy_order_total", summary)
        for stage in (
            "paper.copy_queue_batch",
            "paper.copy_price_lookup",
            "paper.copy_execute_order",
            "paper.copy_order_total",
        ):
            self.assertGreaterEqual(summary[stage]["count"], 1)
            self.assertGreaterEqual(summary[stage]["p50_ms"], 0)


if __name__ == "__main__":
    unittest.main()
