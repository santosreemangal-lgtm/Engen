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


if __name__ == "__main__":
    unittest.main()
