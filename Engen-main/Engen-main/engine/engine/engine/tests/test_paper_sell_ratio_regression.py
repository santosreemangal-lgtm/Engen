import unittest
from datetime import datetime, timezone
from decimal import Decimal

from engine.paper_copy_runner import execute_pending_paper_orders, queue_paper_orders
from engine.paper_simulator import PaperSimulator
from engine.pnl import MarketPrice


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def sell_signal(signature, source_before, source_sold):
    return {
        "signature": signature,
        "status": "success",
        "metadata_complete": True,
        "signers": [WALLET],
        "signature_observation": {"confirmation_status": "finalized", "slot": 1},
        "trade_signal": {
            "classification": "SELL_CANDIDATE",
            "asset_mints": [ASSET],
            "quote_mints": [USDC],
        },
        "token_balance_changes": [
            {
                "owner": WALLET,
                "mint": ASSET,
                "decimals": 0,
                "delta_amount": str(-source_sold),
                "pre_amount": str(source_before),
                "post_amount": str(source_before - source_sold),
            },
            {
                "owner": WALLET,
                "mint": USDC,
                "decimals": 6,
                "delta_amount": "5000000",
            },
        ],
    }


def fresh_market_price(mint):
    observed_at = datetime.now(timezone.utc).isoformat()
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


class PaperSellRatioRegressionTests(unittest.TestCase):
    def test_source_sells_fifty_percent_so_paper_sells_fifty_percent(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000", ASSET: "2"})
        queued = queue_paper_orders(
            WALLET, [sell_signal("sig-half", 10, 5)], simulator
        )
        self.assertEqual(len(queued), 1)
        order = next(iter(simulator.orders.values()))
        self.assertEqual(
            Decimal(order["details"]["source_sell_fraction"]), Decimal("0.5")
        )

        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=fresh_market_price
        )
        self.assertEqual(outcomes[0]["status"], "FILLED")
        report = simulator.report()
        self.assertEqual(Decimal(report["fills"][0]["quantity"]), Decimal("1"))
        self.assertEqual(Decimal(report["balances"][ASSET]), Decimal("1"))
        self.assertFalse(report["real_trading"])
        self.assertFalse(report["transaction_broadcast"])

    def test_source_sells_twenty_five_percent_so_paper_sells_twenty_five_percent(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000", ASSET: "4"})
        queued = queue_paper_orders(
            WALLET, [sell_signal("sig-quarter", 20, 5)], simulator
        )
        self.assertEqual(len(queued), 1)
        order = next(iter(simulator.orders.values()))
        self.assertEqual(
            Decimal(order["details"]["source_sell_fraction"]), Decimal("0.25")
        )
        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=fresh_market_price
        )
        self.assertEqual(outcomes[0]["status"], "FILLED")
        self.assertEqual(Decimal(simulator.report()["fills"][0]["quantity"]), Decimal("1"))
        self.assertEqual(Decimal(simulator.report()["balances"][ASSET]), Decimal("3"))

    def test_sell_signal_does_not_sell_when_paper_position_is_missing(self):
        simulator = PaperSimulator(initial_balances={"USD": "1000"})
        queued = queue_paper_orders(
            WALLET, [sell_signal("sig-no-position", 10, 5)], simulator
        )
        self.assertEqual(len(queued), 1)
        outcomes = execute_pending_paper_orders(
            WALLET, simulator, price_provider=fresh_market_price
        )
        self.assertEqual(outcomes[0]["status"], "WAITING_FOR_POSITION")
        self.assertEqual(simulator.report()["fills"], [])
        self.assertFalse(simulator.report()["real_trading"])
        self.assertFalse(simulator.report()["transaction_broadcast"])


if __name__ == "__main__":
    unittest.main()
