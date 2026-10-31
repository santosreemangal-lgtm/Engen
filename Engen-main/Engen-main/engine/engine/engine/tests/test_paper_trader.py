import subprocess
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from pathlib import Path

from engine.paper_trader import PaperTrader
from engine.transaction_analysis import WRAPPED_SOL_MINT


class PaperTraderTests(unittest.TestCase):

    def make_result(self, side="BUY_CANDIDATE", signature="demo-signature"):
        asset_delta = "500" if side == "BUY_CANDIDATE" else "-250"
        quote_delta = "-1000000" if side == "BUY_CANDIDATE" else "500000"

        return {
            "signature": signature,
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

    def test_paper_ledger_survives_full_python_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "paper_process_restart.db"
            db_literal = repr(str(db_path))

            first_process = (
                "from engine.paper_trader import PaperTrader; "
                "t=PaperTrader({'USDC': '10'}, db_path=" + repr(str(db_path)) + "); "
                "r=t.process({"
                "'signature':'process-restart-signature',"
                "'available':True,'status':'success',"
                "'trade_signal':{'classification':'BUY_CANDIDATE',"
                "'asset_mints':['TOKEN_A'],'quote_mints':['USDC']},"
                "'token_balance_changes':["
                "{'mint':'TOKEN_A','delta_amount':'500','decimals':2},"
                "{'mint':'USDC','delta_amount':'-1000000','decimals':6}]"
                "}); "
                "assert r['status']=='PAPER_FILLED', r"
            )
            subprocess.run(
                [sys.executable, "-c", first_process],
                check=True,
                capture_output=True,
                text=True,
            )

            second_process = (
                "from engine.paper_trader import PaperTrader; "
                "t=PaperTrader({'USDC':'999'}, db_path=" + repr(str(db_path)) + "); "
                "assert t.get_balances()=={'USDC':'9','TOKEN_A':'5'}, t.get_balances(); "
                "assert len(t.trades)==1, t.trades; "
                "r=t.process({"
                "'signature':'process-restart-signature',"
                "'available':True,'status':'success',"
                "'trade_signal':{'classification':'BUY_CANDIDATE',"
                "'asset_mints':['TOKEN_A'],'quote_mints':['USDC']},"
                "'token_balance_changes':["
                "{'mint':'TOKEN_A','delta_amount':'500','decimals':2},"
                "{'mint':'USDC','delta_amount':'-1000000','decimals':6}]"
                "}); "
                "assert r['status']=='DUPLICATE', r; "
                "assert len(t.trades)==1, t.trades; "
                "print('paper-ledger-process-restart: PASS')"
            )
            result = subprocess.run(
                [sys.executable, "-c", second_process],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("paper-ledger-process-restart: PASS", result.stdout)

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

    def test_balance_and_trade_roll_back_together_on_persistence_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "atomic_failure.db"
            trader = PaperTrader({"USDC": "10"}, db_path=db_path)
            before = trader.get_balances()

            with sqlite3.connect(db_path) as connection:
                connection.execute("""
                    CREATE TRIGGER force_balance_failure
                    BEFORE UPDATE ON paper_balances
                    BEGIN
                        SELECT RAISE(ABORT, 'forced persistence failure');
                    END
                """)

            with self.assertRaises(sqlite3.IntegrityError):
                trader.process(self.make_result())

            self.assertEqual(trader.get_balances(), before)
            self.assertEqual(trader.trades, [])
            with sqlite3.connect(db_path) as connection:
                balances = dict(connection.execute(
                    "SELECT mint, amount FROM paper_balances"
                ).fetchall())
                trade_count = connection.execute(
                    "SELECT COUNT(*) FROM paper_trades"
                ).fetchone()[0]
            self.assertEqual(balances, {"USDC": "10"})
            self.assertEqual(trade_count, 0)

    def test_concurrent_duplicate_execution_across_instances_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "concurrent_duplicate.db"
            traders = [
                PaperTrader({"USDC": "10"}, db_path=db_path)
                for _ in range(2)
            ]
            transaction = self.make_result(signature="same-concurrent-signature")
            barrier = threading.Barrier(2)

            def execute(trader):
                barrier.wait()
                return trader.process(transaction)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(execute, traders))

            statuses = sorted(result["status"] for result in results)
            self.assertEqual(statuses, ["DUPLICATE", "PAPER_FILLED"])
            recovered = PaperTrader({"USDC": "999"}, db_path=db_path)
            self.assertEqual(
                recovered.get_balances(),
                {"USDC": "9", "TOKEN_A": "5"},
            )
            self.assertEqual(len(recovered.trades), 1)

    def test_concurrent_distinct_trades_use_latest_committed_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "concurrent_distinct.db"
            traders = [
                PaperTrader({"USDC": "10"}, db_path=db_path)
                for _ in range(2)
            ]
            transactions = [
                self.make_result(signature="concurrent-trade-one"),
                self.make_result(signature="concurrent-trade-two"),
            ]
            barrier = threading.Barrier(2)

            def execute(item):
                trader, transaction = item
                barrier.wait()
                return trader.process(transaction)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(execute, zip(traders, transactions)))

            self.assertEqual(
                [result["status"] for result in results],
                ["PAPER_FILLED", "PAPER_FILLED"],
            )
            recovered = PaperTrader({"USDC": "999"}, db_path=db_path)
            self.assertEqual(
                recovered.get_balances(),
                {"USDC": "8", "TOKEN_A": "10"},
            )
            self.assertEqual(len(recovered.trades), 2)


if __name__ == "__main__":
    unittest.main()