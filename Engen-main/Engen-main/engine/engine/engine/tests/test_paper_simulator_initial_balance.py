import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from engine.paper_simulator import PaperSimulator


class PaperSimulatorInitialBalanceTests(unittest.TestCase):
    def test_initial_balance_is_set_exactly_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paper.db"
            simulator = PaperSimulator(db_path=database)

            result = simulator.set_initial_virtual_balance("1250.50")

            self.assertEqual(result["initial_balance"], "1250.50")
            self.assertEqual(simulator.balances["USD"], Decimal("1250.50"))
            self.assertFalse(result["real_trading"])
            self.assertFalse(result["transaction_broadcast"])

            reopened = PaperSimulator(db_path=database)
            self.assertEqual(reopened.balances["USD"], Decimal("1250.50"))
            self.assertEqual(reopened.report()["initial_balance"], "1250.50")

    def test_updated_order_quantity_survives_database_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paper.db"
            simulator = PaperSimulator(db_path=database)
            order = simulator.create_order(
                "quantity-persistence", "BUY", "TokenMintExample", "1", "USD",
                created_at="2026-10-30T00:00:00+00:00",
            )
            order["quantity"] = "2.5"
            simulator._persist(order)

            reopened = PaperSimulator(db_path=database)
            self.assertEqual(reopened.orders[order["order_id"]]["quantity"], "2.5")

    def test_initial_balance_cannot_be_changed_after_initial_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            simulator = PaperSimulator(db_path=Path(directory) / "paper.db")
            simulator.set_initial_virtual_balance("1000")

            with self.assertRaisesRegex(ValueError, "already been set"):
                simulator.set_initial_virtual_balance("2000")

            # Repeating the same value is safe and idempotent.
            result = simulator.set_initial_virtual_balance("1000")
            self.assertEqual(result["initial_balance"], "1000")
            self.assertEqual(simulator.balances["USD"], Decimal("1000"))


    def test_in_memory_simulator_also_locks_initial_balance(self):
        simulator = PaperSimulator()
        simulator.set_initial_virtual_balance("250")

        with self.assertRaisesRegex(ValueError, "already been set"):
            simulator.set_initial_virtual_balance("500")

        self.assertEqual(simulator.balances["USD"], Decimal("250"))


    def test_initial_balance_cannot_change_after_a_filled_order(self):
        with tempfile.TemporaryDirectory() as directory:
            simulator = PaperSimulator(db_path=Path(directory) / "paper.db")
            simulator.orders["filled-test"] = {"status": "FILLED"}

            with self.assertRaisesRegex(ValueError, "after a paper order has filled"):
                simulator.set_initial_virtual_balance("2000")



    def test_reset_archives_orders_and_fills_and_preserves_legacy_paper_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paper.db"
            simulator = PaperSimulator(db_path=database)
            simulator.set_virtual_balance("1000", "USD")
            created_at = "2026-10-30T00:00:00+00:00"
            order = simulator.create_order(
                "before-reset", "BUY", "TokenMintExample", "1", "USD",
                created_at=created_at,
            )
            filled_order = dict(order, status="FILLED", reason=None)
            fill = {
                "fill_id": "fill-before-reset",
                "order_id": order["order_id"],
                "asset_mint": "TokenMintExample",
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
                "side": "BUY",
                "quote_mint": "USD",
            }
            simulator._persist(
                filled_order,
                balances={"USD": Decimal("990")},
                fill=fill,
            )
            simulator.orders[order["order_id"]] = filled_order
            simulator.balances = {"USD": Decimal("990")}
            simulator.fills.append(fill)
            simulator._rebuild_pnl_ledger()

            with sqlite3.connect(database) as db:
                db.execute("""CREATE TABLE paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signature TEXT NOT NULL UNIQUE,
                    side TEXT NOT NULL,
                    asset_mint TEXT NOT NULL,
                    asset_amount TEXT NOT NULL,
                    quote_mint TEXT NOT NULL,
                    quote_amount TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
                db.execute(
                    """INSERT INTO paper_trades(
                        signature,side,asset_mint,asset_amount,quote_mint,
                        quote_amount,status,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    ("legacy-trade", "BUY", "LegacyMint", "2", "USD",
                     "25", "FILLED", created_at),
                )

            result = simulator.reset_session("100", "USD")
            report = simulator.report()

            self.assertTrue(result["reset"])
            self.assertEqual(result["initial_balance"], "100")
            self.assertEqual(result["archived_orders"], 1)
            self.assertEqual(result["archived_fills"], 1)
            self.assertEqual(report["balances"], {"USD": "100"})
            self.assertEqual(report["initial_balance"], "100")
            self.assertEqual(report["orders"], [])
            self.assertEqual(report["fills"], [])
            self.assertEqual(len(report["archived_sessions"]), 1)
            self.assertEqual(report["archived_orders"][0]["signal_id"], "before-reset")
            self.assertEqual(report["archived_fills"][0]["fill_id"], "fill-before-reset")
            self.assertFalse(report["real_trading"])
            self.assertFalse(report["transaction_broadcast"])

            with sqlite3.connect(database) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0],
                    1,
                )

            reopened = PaperSimulator(db_path=database)
            reopened_report = reopened.report()
            self.assertEqual(reopened_report["balances"], {"USD": "100"})
            self.assertEqual(reopened_report["orders"], [])
            self.assertEqual(len(reopened_report["archived_orders"]), 1)
            self.assertEqual(len(reopened_report["archived_fills"]), 1)

if __name__ == "__main__":
    unittest.main()
