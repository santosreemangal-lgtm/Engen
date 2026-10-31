import unittest
from datetime import datetime, timezone
from decimal import Decimal

from engine.paper_copy_runner import execute_pending_paper_orders
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice


WALLET = "11111111111111111111111111111111"
ASSET = "PaperAssetMint"
NOW = datetime.now(timezone.utc).isoformat()


def make_order(simulator, side="BUY", signal="signal-1", details=None):
    base_details = {
        "origin": "AUTOMATIC_WALLET_COPY",
        "source_wallet": WALLET,
        "source_slot": 10,
        "target_notional_usd": "10" if side == "BUY" else None,
        "sizing_mode": "FIXED_USD",
        "sizing_value": "10",
        "source_sell_fraction": "0.5" if side == "SELL" else None,
    }
    base_details.update(details or {})
    return simulator.create_order(
        signal_id=signal,
        side=side,
        asset_mint=ASSET,
        quantity="1",
        quote_mint="USD",
        created_at=NOW,
        details=base_details,
    )


def fresh_market_price(liquidity="50000"):
    return MarketPrice(
        mint=ASSET,
        price=Decimal("2"),
        currency="USD",
        source="offline-test-fixture",
        observed_at=NOW,
        liquidity_usd=Decimal(liquidity),
        liquidity_source="offline-test-fixture",
        liquidity_observed_at=NOW,
    )


class PaperOrderDiagnosticsTests(unittest.TestCase):
    def test_buy_without_virtual_balance_has_machine_readable_explanation(self):
        simulator = PaperSimulator()
        order = make_order(simulator)

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )

        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_VIRTUAL_BALANCE")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "MISSING_VIRTUAL_USD_BALANCE")
        self.assertIn("virtual USD balance", saved["details"]["diagnostic_next_step"])
        self.assertEqual(saved["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_buy_with_balance_but_no_market_snapshot_explains_provider_blocker(self):
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        order = make_order(simulator)

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )

        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "ASSET_USD_PRICE_UNAVAILABLE")
        self.assertIn("fresh USD price", saved["details"]["diagnostic_next_step"])
        self.assertEqual(saved["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_market_provider_exception_type_is_saved_without_exception_text(self):
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        order = make_order(simulator)

        def provider(_mint):
            raise RuntimeError("private provider detail must not be persisted")

        outcomes = execute_pending_paper_orders(WALLET, simulator, price_provider=provider)

        saved = simulator.orders[order["order_id"]]
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(saved["details"]["diagnostic_code"], "MARKET_DATA_PROVIDER_ERROR")
        self.assertEqual(saved["details"]["diagnostic_error_type"], "RuntimeError")
        self.assertNotIn("private provider detail", saved["reason"])
        self.assertEqual(simulator.fills, [])

    def test_missing_liquidity_keeps_market_data_wait_status_with_specific_code(self):
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        order = make_order(simulator)
        price = MarketPrice(
            mint=ASSET, price=Decimal("2"), currency="USD",
            source="offline-test-fixture", observed_at=NOW,
        )

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: price,
        )

        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "LIQUIDITY_DATA_UNAVAILABLE")
        self.assertEqual(saved["details"]["execution_state"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(simulator.fills, [])

    def test_stale_liquidity_keeps_market_data_wait_status_with_specific_code(self):
        from datetime import timedelta
        stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        order = make_order(simulator)
        price = MarketPrice(
            mint=ASSET, price=Decimal("2"), currency="USD",
            source="offline-test-fixture", observed_at=NOW,
            liquidity_usd=Decimal("50000"), liquidity_source="offline-test-fixture",
            liquidity_observed_at=stale,
        )

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: price,
        )

        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_MARKET_DATA")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "LIQUIDITY_DATA_STALE")
        self.assertEqual(saved["details"]["execution_state"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(simulator.fills, [])

    def test_low_liquidity_has_specific_wait_code_and_never_fills(self):
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        order = make_order(simulator)

        outcomes = execute_pending_paper_orders(
            WALLET, simulator,
            price_provider=lambda mint: fresh_market_price("500"),
        )

        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_LIQUIDITY")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "LIQUIDITY_BELOW_REQUIRED_MINIMUM")
        self.assertIn("liquidity minimum", saved["details"]["diagnostic_next_step"])
        self.assertEqual(saved["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_sell_waits_only_when_an_earlier_paper_buy_is_unresolved(self):
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        buy = make_order(
            simulator, "BUY", "earlier-buy",
            {"source_slot": 10, "target_notional_usd": "10"},
        )
        sell = make_order(
            simulator, "SELL", "later-sell",
            {"source_slot": 11, "target_notional_usd": None},
        )

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )
        by_signal = {item["signal_id"]: item for item in outcomes}

        self.assertEqual(by_signal["later-sell"]["status"], "WAITING_FOR_POSITION")
        saved_sell = simulator.orders[sell["order_id"]]
        self.assertEqual(saved_sell["details"]["diagnostic_code"], "WAITING_FOR_PRIOR_PAPER_BUY")
        self.assertEqual(saved_sell["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_sell_without_any_simulated_position_is_rejected_with_explanation(self):
        simulator = PaperSimulator()
        order = make_order(simulator, "SELL")

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: None
        )

        self.assertEqual(outcomes[0]["status"], "REJECTED")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "NO_SIMULATED_POSITION")
        self.assertIn("existing virtual token balance", saved["details"]["diagnostic_next_step"])
        self.assertEqual(simulator.fills, [])

    def test_simulator_exception_is_classified_without_persisting_exception_text(self):
        simulator = PaperSimulator(initial_balances={"USD": "100"})
        order = make_order(simulator)

        def broken_execute(*_args, **_kwargs):
            raise ValueError("sensitive stack detail")

        simulator.execute_order = broken_execute
        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=lambda mint: fresh_market_price()
        )

        self.assertEqual(outcomes[0]["status"], "EXECUTION_ERROR")
        saved = simulator.orders[order["order_id"]]
        self.assertEqual(saved["details"]["diagnostic_code"], "PAPER_SIMULATOR_EXCEPTION")
        self.assertEqual(saved["details"]["diagnostic_error_type"], "ValueError")
        self.assertNotIn("sensitive stack detail", saved["reason"])
        self.assertEqual(simulator.fills, [])


if __name__ == "__main__":
    unittest.main()
