import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from engine.paper_simulator import PaperSimulator


class PaperSimulatorResetTests(unittest.TestCase):
    def test_reset_starts_a_fresh_persisted_paper_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "paper_trading.db"
            simulator = PaperSimulator(db_path=db_path)
            simulator.set_initial_virtual_balance("1000", "USD")

            created_at = datetime.now(timezone.utc).isoformat()
            order = simulator.create_order(
                "previous-session-signal",
                "BUY",
                "TEST_TOKEN",
                "1",
                "USD",
                created_at=created_at,
            )
            filled_order = dict(order, status="FILLED")
            fill = {
                "fill_id": "previous-session-fill",
                "order_id": order["order_id"],
                "asset_mint": "TEST_TOKEN",
                "quantity": "1",
                "reference_price": "10",
                "execution_price": "10",
                "gross_quote": "10",
                "fee": "0",
                "network_cost": "0",
                "net_quote": "10",
                "currency": "USD",
                "price_source": "unit-test",
                "price_timestamp": created_at,
                "price_age_seconds": 0,
                "slippage_bps": 0,
                "created_at": created_at,
            }
            simulator._persist(
                filled_order,
                balances={"USD": Decimal("990"), "TEST_TOKEN": Decimal("1")},
                fill=fill,
            )
            simulator = PaperSimulator(db_path=db_path)

            with sqlite3.connect(str(db_path)) as db:
                db.execute(
                    "CREATE TABLE paper_balances(mint TEXT PRIMARY KEY, amount TEXT NOT NULL)"
                )
                db.execute(
                    """CREATE TABLE paper_trades(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signature TEXT NOT NULL UNIQUE,
                        side TEXT NOT NULL,
                        asset_mint TEXT NOT NULL,
                        asset_amount TEXT NOT NULL,
                        quote_mint TEXT NOT NULL,
                        quote_amount TEXT,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                db.execute("INSERT INTO paper_balances VALUES('TEST_TOKEN','1')")
                db.execute(
                    """INSERT INTO paper_trades(
                        signature,side,asset_mint,asset_amount,quote_mint,
                        quote_amount,status,created_at
                    ) VALUES('legacy-signal','BUY','TEST_TOKEN','1','USD','10','FILLED',?)""",
                    (created_at,),
                )

            result = simulator.reset_session("250", "USD")
            report = simulator.report()

            self.assertTrue(result["reset"])
            self.assertEqual(result["initial_balance"], "250")
            self.assertEqual(report["initial_balance"], "250")
            self.assertEqual(report["balances"], {"USD": "250"})
            self.assertEqual(report["orders"], [])
            self.assertEqual(report["fills"], [])
            self.assertEqual(report["pnl_events"], [])
            self.assertEqual(simulator.processed_signals, set())
            self.assertEqual(len(report["archived_sessions"]), 1)
            archived = report["archived_sessions"][0]
            self.assertEqual(archived["initial_balance"], "1000")
            self.assertEqual(len(archived["orders"]), 1)
            self.assertEqual(len(archived["fills"]), 1)

            with sqlite3.connect(str(db_path)) as db:
                # Legacy paper trades remain available to the History view.
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 1
                )
                self.assertEqual(
                    dict(db.execute("SELECT mint,amount FROM paper_balances")),
                    {"USD": "250"},
                )

            reopened = PaperSimulator(db_path=db_path)
            reopened_report = reopened.report()
            self.assertEqual(reopened_report["orders"], [])
            self.assertEqual(reopened_report["fills"], [])
            self.assertEqual(len(reopened_report["archived_sessions"]), 1)
            self.assertEqual(
                reopened_report["archived_sessions"][0]["orders"][0]["signal_id"],
                "previous-session-signal",
            )

            # A signal from the previous session can be processed again.
            next_order = simulator.create_order(
                "previous-session-signal",
                "BUY",
                "TEST_TOKEN",
                "1",
                "USD",
                created_at=created_at,
            )
            self.assertEqual(next_order["status"], "CREATED")

    def test_archived_trade_history_retains_only_latest_100_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "paper_trading.db"
            simulator = PaperSimulator(db_path=db_path)
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for index in range(105):
                created_at = (start + timedelta(seconds=index)).isoformat()
                simulator.create_order(
                    f"retention-signal-{index:03d}",
                    "BUY",
                    "TEST_TOKEN",
                    "1",
                    "USD",
                    created_at=created_at,
                )

            simulator.reset_session("100", "USD")
            report = simulator.report()
            self.assertEqual(report["orders"], [])
            self.assertEqual(report["balances"], {"USD": "100"})
            self.assertEqual(len(report["archived_sessions"]), 1)
            archived_orders = report["archived_sessions"][0]["orders"]
            self.assertEqual(len(archived_orders), 100)
            signal_ids = {order["signal_id"] for order in archived_orders}
            self.assertNotIn("retention-signal-000", signal_ids)
            self.assertNotIn("retention-signal-004", signal_ids)
            self.assertIn("retention-signal-005", signal_ids)
            self.assertIn("retention-signal-104", signal_ids)

            reopened = PaperSimulator(db_path=db_path)
            self.assertEqual(
                len(reopened.report()["archived_sessions"][0]["orders"]), 100
            )

    def test_reset_rejects_zero_balance_without_changing_the_session(self):
        simulator = PaperSimulator({"USD": "100"})
        with self.assertRaises(ValueError):
            simulator.reset_session("0", "USD")
        self.assertEqual(simulator.balances["USD"], Decimal("100"))


if __name__ == "__main__":
    unittest.main()
