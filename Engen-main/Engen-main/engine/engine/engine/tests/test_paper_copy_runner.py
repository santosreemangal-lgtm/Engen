import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from engine import paper_copy_runner
from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeSimulator:
    def __init__(self):
        self.orders = {}

    def create_order(self, signal_id, side, asset_mint, quantity, quote_mint, *, created_at, details=None):
        if signal_id in self.orders:
            return {"status": "DUPLICATE", "signal_id": signal_id}
        order = {
            "order_id": "paper-order-1",
            "signal_id": signal_id,
            "side": side,
            "asset_mint": asset_mint,
            "quantity": quantity,
            "quote_mint": quote_mint,
            "status": "CREATED",
            "details": details or {},
        }
        self.orders[signal_id] = order
        return order


def candidate_result(*, confirmation="finalized", classification="BUY_CANDIDATE"):
    asset_delta = "2500000" if classification == "BUY_CANDIDATE" else "-2500000"
    quote_delta = "-10000000" if classification == "BUY_CANDIDATE" else "10000000"
    return {
        "signature": "sig-test-1",
        "status": "success",
        "metadata_complete": True,
        "signers": [WALLET],
        "signature_observation": {
            "confirmation_status": confirmation,
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
                "owner": WALLET, "mint": ASSET, "decimals": 6,
                "delta_amount": asset_delta,
            },
            {
                "owner": WALLET, "mint": QUOTE, "decimals": 6,
                "delta_amount": quote_delta,
            },
        ],
    }


class PaperCopyRunnerTests(unittest.TestCase):
    def test_profile_simulators_are_isolated_and_persistent(self):
        first_profile = str(uuid.uuid4())
        second_profile = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(paper_copy_runner, "PROFILE_DATA_DIR", Path(directory)):
                with patch.dict(paper_copy_runner._PROFILE_SIMULATORS, {}, clear=True):
                    first = paper_copy_runner.get_paper_simulator(first_profile)
                    second = paper_copy_runner.get_paper_simulator(second_profile)
                    self.assertIsNot(first, second)

                    first.set_initial_virtual_balance("1000")
                    second.set_initial_virtual_balance("250")
                    first.add_virtual_balance("USD", "25")

                    self.assertEqual(first.balances["USD"], Decimal("1025"))
                    self.assertEqual(second.balances["USD"], Decimal("250"))
                    self.assertNotEqual(
                        paper_copy_runner.profile_paper_simulator_db_path(first_profile),
                        paper_copy_runner.profile_paper_simulator_db_path(second_profile),
                    )

                    paper_copy_runner._PROFILE_SIMULATORS.clear()
                    restored = paper_copy_runner.get_paper_simulator(first_profile)
                    self.assertEqual(restored.balances["USD"], Decimal("1025"))
                    self.assertEqual(
                        [event["kind"] for event in restored.report()["transactions"]],
                        ["INITIAL_BALANCE", "DEPOSIT"],
                    )

    def test_virtual_usd_balance_adjustment_is_audited_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paper_simulator.db"
            simulator = PaperSimulator(db_path=database)

            with self.assertRaisesRegex(ValueError, "starting virtual USD balance"):
                simulator.set_virtual_balance("500")

            simulator.set_initial_virtual_balance("1000")
            adjustment = simulator.set_virtual_balance("650.25")

            self.assertEqual(adjustment["previous_balance"], "1000")
            self.assertEqual(adjustment["adjustment"], "-349.75")
            self.assertEqual(adjustment["balance"], "650.25")
            self.assertFalse(adjustment["real_trading"])
            self.assertFalse(adjustment["transaction_broadcast"])

            report = simulator.report()
            self.assertEqual(report["initial_balance"], "1000")
            self.assertEqual(report["balances"]["USD"], "650.25")
            self.assertEqual(report["transactions"][-1]["kind"], "BALANCE_ADJUSTMENT")
            self.assertEqual(report["transactions"][-1]["amount"], "-349.75")
            self.assertEqual(report["transactions"][-1]["balance_after"], "650.25")

            restored = PaperSimulator(db_path=database)
            restored_report = restored.report()
            self.assertEqual(restored_report["initial_balance"], "1000")
            self.assertEqual(restored_report["balances"]["USD"], "650.25")
            self.assertEqual(restored_report["transactions"][-1]["kind"], "BALANCE_ADJUSTMENT")

    def test_virtual_usd_balance_adjustment_rejects_invalid_targets(self):
        simulator = PaperSimulator()
        simulator.set_initial_virtual_balance("1000")
        with self.assertRaises(ValueError):
            simulator.set_virtual_balance("-1")
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            simulator.set_virtual_balance("1000000001")
        with self.assertRaisesRegex(ValueError, "Only the virtual USD"):
            simulator.set_virtual_balance("100", mint="SOL")
        self.assertEqual(simulator.balances["USD"], Decimal("1000"))

    def test_confirmed_candidate_creates_deduplicated_paper_order(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [candidate_result()], simulator)

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "BUY")
        self.assertEqual(queued[0]["quantity"], "2.5")
        self.assertEqual(queued[0]["source_quote_mint"], QUOTE)
        self.assertEqual(queued[0]["execution_state"], "AWAITING_VERIFIED_MARKET_DATA")
        self.assertEqual(len(simulator.orders), 1)
        duplicate = queue_paper_orders(WALLET, [candidate_result()], simulator)
        self.assertEqual(duplicate, [])
        order = next(iter(simulator.orders.values()))
        self.assertEqual(order["details"]["origin"], "AUTOMATIC_WALLET_COPY")
        self.assertEqual(order["details"]["source_quote_mint"], QUOTE)
        self.assertEqual(order["quote_mint"], "USD")
        self.assertFalse(order["details"]["real_trading"])
        self.assertFalse(order["details"]["transaction_broadcast"])

    def test_source_percentage_scales_source_token_quantity(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 20},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["quantity"], "0.5")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")

    def test_fixed_usd_size_is_recorded_for_verified_price_conversion(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25"},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["sizing_mode"], "FIXED_USD")
        self.assertEqual(queued[0]["target_notional_usd"], "25")
        order = next(iter(simulator.orders.values()))
        self.assertEqual(order["details"]["target_notional_usd"], "25")

    def test_invalid_sizing_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            queue_paper_orders(
                WALLET, [candidate_result()], FakeSimulator(),
                copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 101},
            )
        with self.assertRaises(ValueError):
            queue_paper_orders(
                WALLET, [candidate_result()], FakeSimulator(),
                copy_settings={"sizing_mode": "UNSUPPORTED", "sizing_value": 10},
            )

    def test_unconfirmed_or_unknown_transaction_is_not_copied(self):
        simulator = FakeSimulator()
        unconfirmed = candidate_result(confirmation="processed")
        unknown = candidate_result(classification="UNKNOWN")
        queued = queue_paper_orders(WALLET, [unconfirmed, unknown], simulator)

        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})

    def test_sell_candidate_maps_to_sell_order(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "SELL")

    def test_buy_only_mode_does_not_queue_sell_orders(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET,
            [candidate_result(classification="SELL_CANDIDATE")],
            simulator,
            allowed_sides="BUY",
        )
        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})

    def test_sell_only_mode_does_not_queue_buy_orders(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET,
            [candidate_result(classification="BUY_CANDIDATE")],
            simulator,
            allowed_sides="SELL",
        )
        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})

    def test_both_mode_allows_buy_and_sell_orders(self):
        buy_simulator = FakeSimulator()
        sell_simulator = FakeSimulator()
        buy = queue_paper_orders(
            WALLET, [candidate_result(classification="BUY_CANDIDATE")],
            buy_simulator, allowed_sides="BOTH",
        )
        sell = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")],
            sell_simulator, allowed_sides="BOTH",
        )
        self.assertEqual([item["side"] for item in buy], ["BUY"])
        self.assertEqual([item["side"] for item in sell], ["SELL"])

    def test_invalid_copy_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            queue_paper_orders(WALLET, [], FakeSimulator(), allowed_sides="SOMETHING")

    def test_pending_copy_order_fills_only_with_fresh_liquid_market_data(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(WALLET, [candidate_result()], simulator)
        self.assertEqual(len(queued), 1)

        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            self.assertEqual(mint, ASSET)
            return MarketPrice(
                mint=mint,
                price=Decimal("4"),
                currency="USD",
                source="test-provider",
                observed_at=observed_at,
                liquidity_usd=Decimal("20000"),
                liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["status"], "FILLED")
        self.assertEqual(report["balances"][ASSET], "2.5")
        self.assertLess(Decimal(report["balances"]["USD"]), Decimal("1000"))
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_fixed_usd_paper_order_uses_fresh_price_to_calculate_quantity(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25"},
        )
        self.assertEqual(len(queued), 1)
        observed_at = datetime.now(timezone.utc).isoformat()

        def provider(mint):
            return MarketPrice(
                mint=mint, price=Decimal("4"), currency="USD",
                source="test-provider", observed_at=observed_at,
                liquidity_usd=Decimal("20000"), liquidity_source="test-provider",
                liquidity_observed_at=observed_at,
            )

        outcomes = execute_pending_paper_orders(WALLET, simulator, provider)
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(report["orders"][0]["quantity"], "6.25")
        self.assertEqual(report["balances"][ASSET], "6.25")
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_missing_market_data_leaves_copy_order_queued(self):
        simulator = PaperSimulator()
        simulator.add_virtual_balance("USD", "1000")
        queue_paper_orders(WALLET, [candidate_result()], simulator)
        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(simulator.report()["orders"][0]["status"], "CREATED")

    def test_only_recognized_quote_tokens_are_eligible(self):
        simulator = FakeSimulator()
        result = candidate_result()
        result["trade_signal"]["quote_mints"] = ["unrecognized-quote"]
        queued = queue_paper_orders(WALLET, [result], simulator)
        self.assertEqual(queued, [])
        self.assertEqual(simulator.orders, {})


if __name__ == "__main__":
    unittest.main()
