import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from engine.pnl import AverageCostLedger, MarketPrice, portfolio_valuation_usd
from engine.paper_simulator import PaperSimulator


NOW = "2026-10-26T12:00:00Z"
MINT = "TOKEN_A"
QUOTE = "USD_STABLE"


def price(value="10", timestamp=NOW, source="test-price-feed",
          liquidity_usd="50000", liquidity_timestamp=NOW):
    return MarketPrice(
        MINT, Decimal(value), "USD", source, timestamp,
        liquidity_usd=liquidity_usd,
        liquidity_source="test-liquidity-feed" if liquidity_usd is not None else None,
        liquidity_observed_at=liquidity_timestamp if liquidity_usd is not None else None,
    )


class PnLTests(unittest.TestCase):
    def test_average_cost_realized_profit_known_inputs(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "10", "5")
        ledger.buy(MINT, "10", "7")
        sale = ledger.sell(MINT, "5", "10")
        self.assertEqual(Decimal(sale["allocated_cost_basis"]), Decimal("30"))
        self.assertEqual(Decimal(sale["realized_pnl"]), Decimal("20"))
        self.assertEqual(ledger.snapshot(MINT)["quantity"], "15")

    def test_average_cost_realized_loss_known_inputs(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "2", "10")
        sale = ledger.sell(MINT, "1", "7")
        self.assertEqual(Decimal(sale["realized_pnl"]), Decimal("-3"))

    def test_unrealized_profit_uses_fresh_timestamped_price(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "3", "4")
        result = ledger.unrealized_pnl(MINT, price("6"), now=NOW)
        self.assertEqual(Decimal(result["pnl"]), Decimal("6"))
        self.assertEqual(result["price_source"], "test-price-feed")
        self.assertEqual(result["price_age_seconds"], 0)

    def test_unrealized_roi_uses_remaining_cost_basis_and_loss_is_negative(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "2", "10", fee="2")
        result = ledger.unrealized_pnl(MINT, price("13.2"), now=NOW)
        self.assertEqual(Decimal(result["pnl"]), Decimal("4.4"))
        self.assertEqual(Decimal(result["cost_basis"]), Decimal("22"))
        self.assertEqual(Decimal(result["roi_pct"]), Decimal("20"))
        loss = ledger.unrealized_pnl(MINT, price("9"), now=NOW)
        self.assertEqual(Decimal(loss["roi_pct"]), Decimal("-18.18181818181818181818181818"))

    def test_missing_price_does_not_become_zero_valuation(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "3", "4")
        result = ledger.unrealized_pnl(MINT, None, now=NOW)
        self.assertIsNone(result["pnl"])
        self.assertEqual(result["status"], "price_unavailable")

    def test_stale_price_is_rejected(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "3", "4")
        stale = price("6", "2026-10-26T11:00:00Z")
        result = ledger.unrealized_pnl(MINT, stale, now=NOW, max_age_seconds=60)
        self.assertIsNone(result["pnl"])
        self.assertEqual(result["status"], "stale_price")
        self.assertEqual(result["price_age_seconds"], 3600)

    def test_unknown_airdrop_basis_is_not_fabricated_as_zero(self):
        ledger = AverageCostLedger()
        ledger.airdrop(MINT, "5", timestamp=NOW)
        self.assertIsNone(ledger.snapshot(MINT)["cost_basis"])
        result = ledger.unrealized_pnl(MINT, price("2"), now=NOW)
        self.assertIsNone(result["pnl"])
        self.assertEqual(result["status"], "unknown_cost_basis")
        sale = ledger.sell(MINT, "1", "3")
        self.assertIsNone(sale["realized_pnl"])

    def test_fee_is_counted_once_on_buy_and_sell(self):
        ledger = AverageCostLedger()
        buy = ledger.buy(MINT, "2", "10", fee="1")
        self.assertEqual(Decimal(buy["cash_cost"]), Decimal("21"))
        # If the supplied unit price is already net of fees, fee_included avoids
        # subtracting the same fee a second time.
        sell = ledger.sell(MINT, "1", "15", fee="2", fee_included=True)
        self.assertEqual(Decimal(sell["cash_proceeds"]), Decimal("15"))
        self.assertEqual(Decimal(sell["realized_pnl"]), Decimal("4.5"))

    def test_network_fee_reduces_sale_proceeds(self):
        ledger = AverageCostLedger()
        ledger.buy(MINT, "1", "5")
        sale = ledger.sell(MINT, "1", "8", network_fee="0.5")
        self.assertEqual(Decimal(sale["realized_pnl"]), Decimal("2.5"))

    def test_deposit_withdrawal_and_internal_transfer_are_not_trade_profit(self):
        ledger = AverageCostLedger()
        deposit = ledger.deposit(MINT, "4", unit_price="3")
        withdrawal = ledger.withdrawal(MINT, "1")
        transfer = ledger.internal_transfer(MINT, "1", from_wallet="wallet-A",
                                            to_wallet="wallet-B", timestamp=NOW)
        self.assertEqual(deposit["trading_pnl"], "0")
        self.assertEqual(withdrawal["trading_pnl"], "0")
        self.assertEqual(transfer["trading_pnl"], "0")
        self.assertEqual(ledger.snapshot(MINT)["quantity"], "3")

    def test_unsupported_currency_and_invalid_price_are_rejected(self):
        with self.assertRaises(ValueError):
            AverageCostLedger(currency="EUR")
        with self.assertRaises(ValueError):
            MarketPrice(MINT, "0", "USD", "source", NOW)
        with self.assertRaises(ValueError):
            MarketPrice(MINT, "1", "USD", "", NOW)
        with self.assertRaises(ValueError):
            MarketPrice(MINT, "1", "USD", "source", "2026-10-26T12:00:00")

    def test_portfolio_value_includes_cash_and_fresh_token_holdings_without_cost_basis(self):
        token_price = MarketPrice(MINT, Decimal("12"), "USD", "test-feed", NOW)
        report = portfolio_valuation_usd(
            {"USD": "100", MINT: "2"},
            {MINT: token_price},
            max_age_seconds=60,
            now=NOW,
        )
        self.assertEqual(report["cash_balance_usd"], "100")
        self.assertEqual(report["token_holdings_value_usd"], "24")
        self.assertEqual(report["portfolio_value_usd"], "124")
        self.assertEqual(report["portfolio_value_status"], "calculated")
        self.assertEqual(report["token_holdings"][0]["quantity"], "2")
        self.assertEqual(report["token_holdings"][0]["price_usd"], "12")
        self.assertEqual(report["token_holdings"][0]["market_value_usd"], "24")

    def test_missing_token_price_withholds_total_but_keeps_each_holding_visible(self):
        fresh = MarketPrice(MINT, Decimal("12"), "USD", "test-feed", NOW)
        report = portfolio_valuation_usd(
            {"USD": "100", MINT: "2", "TOKEN_B": "3"},
            {MINT: fresh},
            max_age_seconds=60,
            now=NOW,
        )
        self.assertEqual(report["cash_balance_usd"], "100")
        self.assertIsNone(report["token_holdings_value_usd"])
        self.assertIsNone(report["portfolio_value_usd"])
        self.assertEqual(report["portfolio_value_status"], "incomplete_market_valuation")
        by_mint = {item["mint"]: item for item in report["token_holdings"]}
        self.assertEqual(by_mint[MINT]["market_value_usd"], "24")
        self.assertIsNone(by_mint["TOKEN_B"]["market_value_usd"])
        self.assertEqual(by_mint["TOKEN_B"]["status"], "price_unavailable")

    def test_future_price_is_not_accepted_for_portfolio_valuation(self):
        future = MarketPrice(MINT, Decimal("12"), "USD", "test-feed", "2026-10-26T12:00:01Z")
        self.assertFalse(future.is_fresh(max_age_seconds=60, now=NOW))
        report = portfolio_valuation_usd(
            {"USD": "100", MINT: "2"},
            {MINT: future},
            max_age_seconds=60,
            now=NOW,
        )
        self.assertIsNone(report["portfolio_value_usd"])
        self.assertEqual(report["token_holdings"][0]["status"], "future_price")

    def test_empty_token_portfolio_is_valued_at_cash_balance(self):
        report = portfolio_valuation_usd({"USD": "7.25"}, {}, now=NOW)
        self.assertEqual(report["cash_balance_usd"], "7.25")
        self.assertEqual(report["token_holdings_value_usd"], "0")
        self.assertEqual(report["portfolio_value_usd"], "7.25")
        self.assertEqual(report["token_holdings"], [])

    def test_stale_price_is_retained_as_stale_but_not_included_in_value(self):
        stale = MarketPrice(
            MINT, Decimal("12"), "USD", "test-feed", "2026-10-26T11:58:59Z"
        )
        report = portfolio_valuation_usd(
            {"USD": "100", MINT: "2"},
            {MINT: stale},
            max_age_seconds=60,
            now=NOW,
        )
        holding = report["token_holdings"][0]
        self.assertEqual(holding["status"], "stale_price")
        self.assertEqual(holding["price_usd"], "12")
        self.assertIsNone(holding["market_value_usd"])
        self.assertIsNone(report["portfolio_value_usd"])

    def test_mismatched_price_mint_is_never_used(self):
        other = MarketPrice("TOKEN_B", Decimal("12"), "USD", "test-feed", NOW)
        report = portfolio_valuation_usd(
            {"USD": "100", MINT: "2"},
            {MINT: other},
            max_age_seconds=60,
            now=NOW,
        )
        holding = report["token_holdings"][0]
        self.assertEqual(holding["status"], "invalid_or_mismatched_price")
        self.assertIsNone(holding["price_usd"])
        self.assertIsNone(report["portfolio_value_usd"])


class PaperSimulatorTests(unittest.TestCase):
    def make_simulator(self, directory=None):
        path = Path(directory) / "simulator.db" if directory else None
        return PaperSimulator({QUOTE: "1000"}, db_path=path)

    def create(self, simulator, signal="signal-1", side="BUY", quantity="2"):
        return simulator.create_order(signal, side, MINT, quantity, QUOTE, created_at=NOW)

    def test_simulated_buy_applies_slippage_fee_and_network_cost(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(order["order_id"], price("10"), now=NOW,
                                         slippage_bps=100, fee_bps=100, network_cost="0.5")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(Decimal(result["fill"]["execution_price"]), Decimal("10.1"))
        self.assertEqual(Decimal(result["fill"]["gross_quote"]), Decimal("20.2"))
        self.assertEqual(Decimal(result["fill"]["fee"]), Decimal("0.202"))
        self.assertEqual(Decimal(result["fill"]["net_quote"]), Decimal("20.902"))
        self.assertFalse(result["real_trading"])
        self.assertFalse(result["transaction_broadcast"])
        self.assertEqual(simulator.balances[MINT], Decimal("2"))

    def test_simulated_sell_slippage_and_costs_are_deterministic(self):
        simulator = PaperSimulator({QUOTE: "0", MINT: "2"})
        order = self.create(simulator, side="SELL")
        result = simulator.execute_order(order["order_id"], price("10"), now=NOW,
                                         slippage_bps=100, fee_bps=100, network_cost="0.5")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(Decimal(result["fill"]["execution_price"]), Decimal("9.9"))
        self.assertEqual(Decimal(result["fill"]["net_quote"]), Decimal("19.102"))

    def test_stale_price_rejects_order_without_fill(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(order["order_id"],
                    price("10", "2026-10-26T11:00:00Z"), now=NOW, max_price_age_seconds=60)
        self.assertEqual(result["status"], "WAITING_FOR_MARKET_DATA")
        self.assertIn("stale", result["reason"])
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_rpc_failure_rejects_without_fill(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(order["order_id"], price(), now=NOW, rpc_ok=False)
        self.assertEqual(result["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_missing_price_waits_without_fabricated_fill(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(order["order_id"], None, now=NOW)
        self.assertEqual(result["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_duplicate_signal_cannot_create_duplicate_order_or_fill(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        duplicate = self.create(simulator)
        self.assertEqual(duplicate["status"], "DUPLICATE")
        simulator.execute_order(order["order_id"], price(), now=NOW)
        self.assertEqual(len(simulator.fills), 1)
        self.assertEqual(len(simulator.orders), 1)

    def test_missing_liquidity_is_rejected_by_default(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(
            order["order_id"], price(liquidity_usd=None), now=NOW
        )
        self.assertEqual(result["status"], "WAITING_FOR_MARKET_DATA")
        self.assertIn("Liquidity data unavailable", result["reason"])
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_stale_liquidity_is_rejected(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(
            order["order_id"], price(liquidity_timestamp="2026-10-26T11:00:00Z"),
            now=NOW,
        )
        self.assertEqual(result["status"], "WAITING_FOR_MARKET_DATA")
        self.assertIn("Liquidity data is stale", result["reason"])
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_low_liquidity_is_rejected(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(
            order["order_id"], price(liquidity_usd="9999.99"), now=NOW,
            min_liquidity_usd="10000",
        )
        self.assertEqual(result["status"], "WAITING_FOR_MARKET_DATA")
        self.assertIn("below the configured USD minimum", result["reason"])
        self.assertEqual(order["status"], "CREATED")
        self.assertEqual(simulator.fills, [])

    def test_transient_market_data_failure_can_retry_and_fill(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        waiting = simulator.execute_order(
            order["order_id"], price("10", "2026-10-26T11:00:00Z"),
            now=NOW, max_price_age_seconds=60,
        )
        self.assertEqual(waiting["status"], "WAITING_FOR_MARKET_DATA")
        self.assertEqual(simulator.orders[order["order_id"]]["status"], "CREATED")
        filled = simulator.execute_order(order["order_id"], price("10"), now=NOW)
        self.assertEqual(filled["status"], "FILLED")
        self.assertEqual(len(simulator.fills), 1)

    def test_sufficient_fresh_liquidity_allows_paper_fill(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        result = simulator.execute_order(
            order["order_id"], price(liquidity_usd="10000"), now=NOW,
            min_liquidity_usd="10000",
        )
        self.assertEqual(result["status"], "FILLED")

    def test_insufficient_balance_is_rejected(self):
        simulator = PaperSimulator({QUOTE: "1"})
        order = self.create(simulator, quantity="100")
        result = simulator.execute_order(order["order_id"], price(), now=NOW)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(simulator.fills, [])
        self.assertEqual(simulator.balances[QUOTE], Decimal("1"))

    def test_created_order_can_be_cancelled_before_execution(self):
        simulator = self.make_simulator()
        order = self.create(simulator)
        cancelled = simulator.cancel_order(order["order_id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        result = simulator.execute_order(order["order_id"], price(), now=NOW)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(simulator.fills, [])

    def test_restart_preserves_orders_fills_balances_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            simulator = self.make_simulator(directory)
            order = self.create(simulator)
            simulator.execute_order(order["order_id"], price(), now=NOW)
            restarted = PaperSimulator({QUOTE: "999"}, db_path=Path(directory) / "simulator.db")
            self.assertEqual(restarted.balances[MINT], Decimal("2"))
            self.assertEqual(len(restarted.orders), 1)
            self.assertEqual(len(restarted.fills), 1)
            self.assertEqual(self.create(restarted)["status"], "DUPLICATE")

    def test_realized_roi_uses_cost_basis_allocated_to_sold_units(self):
        fills = [
            {"side": "BUY", "asset_mint": MINT, "quantity": "2",
             "execution_price": "10", "fee": "2", "network_cost": "0"},
            {"side": "SELL", "asset_mint": MINT, "quantity": "1",
             "execution_price": "16.5", "fee": "1.65", "network_cost": "0"},
        ]
        from engine.pnl import realized_pnl_from_fills
        result = realized_pnl_from_fills(fills)[MINT]
        self.assertEqual(Decimal(result["pnl"]), Decimal("3.85"))
        self.assertEqual(Decimal(result["cost_basis_sold"]), Decimal("11"))
        self.assertEqual(Decimal(result["roi_pct"]), Decimal("35"))

    def test_unknown_realized_basis_has_no_roi(self):
        fills = [{
            "side": "SELL", "asset_mint": MINT, "quantity": "1",
            "execution_price": "15", "fee": "0", "network_cost": "0",
        }]
        from engine.pnl import realized_pnl_from_fills
        result = realized_pnl_from_fills(fills)[MINT]
        self.assertIsNone(result["pnl"])
        self.assertIsNone(result["cost_basis_sold"])
        self.assertIsNone(result["roi_pct"])
        self.assertEqual(result["status"], "unknown_cost_basis")

    def test_realized_pnl_is_reproducible_from_fills_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "simulator.db"
            simulator = PaperSimulator({QUOTE: "1000"}, db_path=db_path)
            buy = simulator.create_order("buy-1", "BUY", MINT, "2", QUOTE, created_at=NOW)
            simulator.execute_order(buy["order_id"], price("10"), now=NOW,
                                    slippage_bps=0, fee_bps=0, network_cost="0")
            sell = simulator.create_order("sell-1", "SELL", MINT, "1", QUOTE, created_at=NOW)
            simulator.execute_order(sell["order_id"], price("15"), now=NOW,
                                    slippage_bps=0, fee_bps=0, network_cost="0")
            summary = simulator.pnl_summary()
            self.assertEqual(Decimal(summary["realized_by_mint"][MINT]["pnl"]), Decimal("5"))
            self.assertEqual(summary["realized_by_mint"][MINT]["status"], "calculated")
            self.assertEqual(Decimal(summary["realized_by_mint"][MINT]["cost_basis_sold"]), Decimal("10"))
            self.assertEqual(Decimal(summary["realized_by_mint"][MINT]["roi_pct"]), Decimal("50"))
            self.assertEqual(Decimal(summary["realized_roi_pct"]), Decimal("50"))
            self.assertIn("unavailable", summary["unrealized_status"])

            restarted = PaperSimulator({QUOTE: "1"}, db_path=db_path)
            restarted_summary = restarted.pnl_summary()
            self.assertEqual(restarted_summary, summary)
            self.assertEqual(len(restarted.fills), 2)

    def test_virtual_funding_is_persisted_and_additive(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "simulator.db"
            simulator = PaperSimulator({}, db_path=db_path)
            first = simulator.add_virtual_balance(QUOTE, "1000")
            second = simulator.add_virtual_balance(QUOTE, "250")
            self.assertEqual(first["balance"], "1000")
            self.assertEqual(second["balance"], "1250")
            self.assertFalse(second["real_trading"])
            self.assertFalse(second["transaction_broadcast"])
            restarted = PaperSimulator({}, db_path=db_path)
            self.assertEqual(restarted.balances[QUOTE], Decimal("1250"))

    def test_virtual_funding_rejects_invalid_values(self):
        simulator = PaperSimulator()
        with self.assertRaises(ValueError):
            simulator.add_virtual_balance("", "100")
        with self.assertRaises(ValueError):
            simulator.add_virtual_balance(QUOTE, "-1")
        with self.assertRaises(ValueError):
            simulator.add_virtual_balance(QUOTE, "NaN")

    def test_report_explicitly_separates_simulation_from_real_balances(self):
        report = self.make_simulator().report()
        self.assertEqual(report["mode"], "PAPER_SIMULATION")
        self.assertTrue(report["simulated"])
        self.assertFalse(report["real_wallet_balances_included"])
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_order_validation_rejects_invalid_side_and_price_settings(self):
        simulator = self.make_simulator()
        with self.assertRaises(ValueError):
            simulator.create_order("bad", "HOLD", MINT, "1", QUOTE, created_at=NOW)
        order = self.create(simulator)
        with self.assertRaises(ValueError):
            simulator.execute_order(order["order_id"], price(), now=NOW, slippage_bps=10001)


if __name__ == "__main__":
    unittest.main()
