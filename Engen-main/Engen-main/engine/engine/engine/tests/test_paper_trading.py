import unittest

from engine.paper_trading import PaperTrader, PaperTradingError


class PaperTraderTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(starting_balance=1000, max_position_fraction=0.5)

    def test_buy_sell_and_realized_pnl(self):
        buy = self.trader.buy("TOKEN", 2, 100)
        sell = self.trader.sell("TOKEN", 1, 125)
        status = self.trader.status()
        self.assertEqual(buy["side"], "buy")
        self.assertEqual(sell["realized_pnl"], 25)
        self.assertEqual(status["cash"], 925)
        self.assertEqual(status["positions"]["TOKEN"]["quantity"], 1)
        self.assertFalse(status["live_trading_enabled"])

    def test_rejects_position_above_risk_limit(self):
        with self.assertRaises(PaperTradingError):
            self.trader.buy("TOKEN", 6, 100)

    def test_rejects_overselling_and_insufficient_cash(self):
        with self.assertRaises(PaperTradingError):
            self.trader.sell("TOKEN", 1, 10)
        with self.assertRaises(PaperTradingError):
            self.trader.buy("TOKEN", 6, 1000)

    def test_pause_blocks_trades_and_resume_allows_them(self):
        self.trader.pause()
        with self.assertRaises(PaperTradingError):
            self.trader.buy("TOKEN", 1, 10)
        self.trader.resume()
        self.trader.buy("TOKEN", 1, 10)
        self.assertEqual(len(self.trader.history()), 1)

    def test_invalid_values_are_rejected(self):
        for args in [
            ("", 1, 10),
            ("TOKEN", 0, 10),
            ("TOKEN", 1, -1),
            ("TOKEN", float("nan"), 10),
            ("TOKEN", 1, float("inf")),
        ]:
            with self.assertRaises(PaperTradingError):
                self.trader.buy(*args)


if __name__ == "__main__":
    unittest.main()
