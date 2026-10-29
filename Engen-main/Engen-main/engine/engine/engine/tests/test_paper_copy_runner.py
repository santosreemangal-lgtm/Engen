import unittest
from datetime import datetime, timezone
from decimal import Decimal

from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeSimulator:
    def __init__(self):
        self.orders = {}
        self.balances = {"USD": Decimal("1000"), ASSET: Decimal("10")}

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
    asset_pre = "0" if classification == "BUY_CANDIDATE" else "10000000"
    asset_post = "2500000" if classification == "BUY_CANDIDATE" else "7500000"
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
                "pre_amount": asset_pre,
                "post_amount": asset_post,
            },
            {
                "owner": WALLET, "mint": QUOTE, "decimals": 6,
                "delta_amount": quote_delta,
            },
        ],
    }


class PaperCopyRunnerTests(unittest.TestCase):
    def test_aggregates_multiple_token_accounts_for_same_asset_and_quote(self):
        result = candidate_result()
        asset, quote = result["token_balance_changes"]
        result["token_balance_changes"] = [
            {**asset, "delta_amount": "1000000", "pre_amount": "0", "post_amount": "1000000"},
            {**asset, "delta_amount": "1500000", "pre_amount": "0", "post_amount": "1500000"},
            {**quote, "delta_amount": "-4000000"},
            {**quote, "delta_amount": "-6000000"},
        ]
        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [result], simulator)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "BUY")
        self.assertEqual(queued[0]["target_notional_usd"], "10")

    def test_confirmed_candidate_creates_deduplicated_paper_order(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(WALLET, [candidate_result()], simulator)

        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "BUY")
        self.assertEqual(queued[0]["quantity"], "1")
        self.assertEqual(queued[0]["target_notional_usd"], "10")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")
        self.assertEqual(queued[0]["sizing_value"], "5")
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
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 20, "max_buy_usd": 1000},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["quantity"], "1")
        self.assertEqual(queued[0]["target_notional_usd"], "200")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")

    def test_fixed_usd_size_is_recorded_for_verified_price_conversion(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25", "max_buy_usd": 1000},
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

    def test_global_maximum_caps_fixed_and_percentage_buys(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 20, "max_buy_usd": 10},
        )
        self.assertEqual(queued[0]["target_notional_usd"], "10")
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_PERCENT")

    def test_mirror_amount_uses_stable_quote_notional(self):
        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [candidate_result()], simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": 10},
        )
        self.assertEqual(queued[0]["target_notional_usd"], "10")

    def test_sell_uses_source_position_fraction_not_buy_sizing(self):
        simulator = FakeSimulator()
        simulator.balances[ASSET] = Decimal("10")
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": 100, "max_buy_usd": 5},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["side"], "SELL")
        self.assertEqual(Decimal(queued[0]["quantity"]), Decimal("1"))
        order = next(iter(simulator.orders.values()))
        self.assertEqual(Decimal(order["details"]["source_sell_fraction"]), Decimal("0.25"))
        self.assertEqual(queued[0]["sizing_mode"], "SOURCE_POSITION_REDUCTION")
        self.assertIsNone(queued[0]["target_notional_usd"])

    def test_sell_execution_uses_fraction_of_current_paper_position(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000", ASSET: "10"})
        queued = queue_paper_orders(
            WALLET, [candidate_result(classification="SELL_CANDIDATE")], simulator,
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": 100, "max_buy_usd": 5},
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
        self.assertEqual(Decimal(simulator.report()["fills"][0]["quantity"]), Decimal("2.5"))
        self.assertEqual(Decimal(simulator.report()["balances"][ASSET]), Decimal("7.5"))
        self.assertFalse(simulator.report()["real_trading"])
        self.assertFalse(simulator.report()["transaction_broadcast"])

    def test_sell_without_known_source_pre_balance_is_skipped(self):
        simulator = FakeSimulator()
        result = candidate_result(classification="SELL_CANDIDATE")
        result["token_balance_changes"][0].pop("pre_amount")
        queued = queue_paper_orders(WALLET, [result], simulator)
        self.assertEqual(queued, [])

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
            copy_settings={"sizing_mode": "FIXED_USD", "sizing_value": "25", "max_buy_usd": 1000},
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

    def test_unknown_quote_mint_can_use_fixed_virtual_sizing_but_not_mirror_usd(self):
        result = candidate_result()
        unknown_quote = "UnknownQuoteMint111111111111111111111111"
        result["trade_signal"]["quote_mints"] = [unknown_quote]
        result["token_balance_changes"][1]["mint"] = unknown_quote

        simulator = FakeSimulator()
        queued = queue_paper_orders(
            WALLET, [result], simulator,
            copy_settings={"sizing_mode": "SOURCE_PERCENT", "sizing_value": 5, "max_buy_usd": 10},
        )
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["target_notional_usd"], "10")

        mirror_simulator = FakeSimulator()
        mirrored = queue_paper_orders(
            WALLET, [result], mirror_simulator,
            copy_settings={"sizing_mode": "MIRROR_AMOUNT", "sizing_value": 10, "max_buy_usd": 10},
        )
        self.assertEqual(mirrored, [])
        self.assertEqual(mirror_simulator.orders, {})


if __name__ == "__main__":
    unittest.main()
