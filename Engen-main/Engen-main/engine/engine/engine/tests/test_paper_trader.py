import tempfile
import unittest
from pathlib import Path

from engine.paper_trader import PaperTrader
from engine.transaction_analysis import WRAPPED_SOL_MINT


class PaperTraderTests(unittest.TestCase):

    def make_result(self, side="BUY_CANDIDATE"):
        asset_delta = "500" if side == "BUY_CANDIDATE" else "-250"
        quote_delta = "-1000000" if side == "BUY_CANDIDATE" else "500000"

        return {
            "signature": "demo-signature",
            "available": True,
            "status": "success",
            "trade_signal": {
                "classification": side,
                "asset_mints": ["TOKEN_A"],
                "quote_mints": ["USDC"],
            },
            "token_balance_changes": [
                {
                    "mint": "TOKEN_A",
                    "delta_amount": asset_delta,
                    "decimals": 2,
                },
                {
                    "mint": "USDC",
                    "delta_amount": quote_delta,
                    "decimals": 6,
                },
            ],
        }

    def test_buy_updates_virtual_balances(self):
        trader = PaperTrader({"USDC": 10})
        result = trader.process(self.make_result())

        self.assertEqual(result["status"], "PAPER_FILLED")
        self.assertEqual(
            trader.get_balances(),
            {"USDC": "9", "TOKEN_A": "5"},
        )

    def test_duplicate_signature_is_rejected(self):
        trader = PaperTrader({"USDC": 10})
        transaction = self.make_result()

        trader.process(transaction)
        result = trader.process(transaction)

        self.assertEqual(result["status"], "DUPLICATE")
        self.assertEqual(len(trader.trades), 1)

    def test_insufficient_quote_balance_is_rejected(self):
        trader = PaperTrader({"USDC": "0.25"})
        result = trader.process(self.make_result())

        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(
            trader.get_balances(),
            {"USDC": "0.25"},
        )

    def test_unknown_signal_is_skipped(self):
        trader = PaperTrader({"USDC": 10})
        transaction = self.make_result("UNKNOWN")
        result = trader.process(transaction)

        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual(
            trader.get_balances(),
            {"USDC": "10"},
        )

    def test_persistent_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "paper.db"

            trader = PaperTrader(
                {"USDC": 10},
                db_path=db_path,
            )
            transaction = self.make_result()

            result = trader.process(transaction)
            self.assertEqual(result["status"], "PAPER_FILLED")

            restarted = PaperTrader(
                {"USDC": 999},
                db_path=db_path,
            )

            self.assertEqual(
                restarted.get_balances(),
                {"USDC": "9", "TOKEN_A": "5"},
            )
            self.assertEqual(len(restarted.trades), 1)

            duplicate = restarted.process(transaction)
            self.assertEqual(duplicate["status"], "DUPLICATE")
            self.assertEqual(len(restarted.trades), 1)

    def test_profile_max_position_percentage_rejects_oversized_buy(self):
        trader = PaperTrader({"USDC": 10}, max_position_pct=5)
        result = trader.process(self.make_result())

        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("maximum position percentage", result["reason"])
        self.assertEqual(trader.get_balances(), {"USDC": "10"})
        self.assertEqual(trader.trades, [])

    def test_profile_max_open_positions_rejects_new_asset(self):
        trader = PaperTrader(
            {"USDC": 10, "TOKEN_B": 1},
            max_open_positions=1,
        )
        result = trader.process(self.make_result())

        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("maximum open paper positions", result["reason"])
        self.assertEqual(trader.get_balances(), {"USDC": "10", "TOKEN_B": "1"})

    def test_profile_risk_limits_are_validated(self):
        for value in (0, -1, 100.1, "nan", "invalid"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PaperTrader({"USDC": 10}, max_position_pct=value)
        for value in (0, -1, 101, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PaperTrader({"USDC": 10}, max_open_positions=value)

    def test_usdt_persistence_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            usdt = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
            db_path = Path(directory) / "usdt-paper.db"

            trader = PaperTrader(
                {usdt: "100"},
                db_path=db_path,
            )
            transaction = self.make_result()

            transaction["trade_signal"]["quote_mints"] = [usdt]
            transaction["token_balance_changes"][1]["mint"] = usdt

            result = trader.process(transaction)
            self.assertEqual(result["status"], "PAPER_FILLED")

            restarted = PaperTrader(
                {usdt: "999"},
                db_path=db_path,
            )

            self.assertEqual(
                restarted.get_balances(),
                {usdt: "99", "TOKEN_A": "5"},
            )
            self.assertEqual(len(restarted.trades), 1)

            duplicate = restarted.process(transaction)
            self.assertEqual(duplicate["status"], "DUPLICATE")
            self.assertEqual(len(restarted.trades), 1)

    def test_native_sol_quote_updates_virtual_balances_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "native-sol-paper.db"

            trader = PaperTrader(
                {WRAPPED_SOL_MINT: "1"},
                db_path=db_path,
            )

            transaction = {
                "signature": "native-sol-buy-signature",
                "available": True,
                "status": "success",
                "trade_signal": {
                    "classification": "BUY_CANDIDATE",
                    "asset_mints": ["TOKEN_A"],
                    "quote_mints": [WRAPPED_SOL_MINT],
                },
                "token_balance_changes": [
                    {
                        "mint": "TOKEN_A",
                        "delta_amount": "12851247",
                        "decimals": 6,
                        "owner": "WALLET_A",
                    },
                    {
                        "mint": WRAPPED_SOL_MINT,
                        "delta_amount": "-200000000",
                        "decimals": 9,
                        "owner": "WALLET_A",
                        "source": "native_sol",
                        "fee_adjusted": True,
                    },
                ],
            }

            result = trader.process(transaction)

            self.assertEqual(result["status"], "PAPER_FILLED")
            self.assertEqual(result["side"], "BUY")
            self.assertEqual(result["quote_amount"], "0.2")
            self.assertEqual(
                trader.get_balances(),
                {
                    WRAPPED_SOL_MINT: "0.8",
                    "TOKEN_A": "12.851247",
                },
            )
            self.assertEqual(len(trader.trades), 1)

            # Restarting must restore balances instead of resetting to 999.
            restarted = PaperTrader(
                {WRAPPED_SOL_MINT: "999"},
                db_path=db_path,
            )

            self.assertEqual(
                restarted.get_balances(),
                {
                    WRAPPED_SOL_MINT: "0.8",
                    "TOKEN_A": "12.851247",
                },
            )
            self.assertEqual(len(restarted.trades), 1)

            duplicate = restarted.process(transaction)
            self.assertEqual(duplicate["status"], "DUPLICATE")
            self.assertEqual(len(restarted.trades), 1)


if __name__ == "__main__":
    unittest.main()