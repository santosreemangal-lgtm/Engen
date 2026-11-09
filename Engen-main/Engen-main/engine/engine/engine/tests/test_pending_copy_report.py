"""Tests for dashboard pending-copy report helper."""
import tempfile
import unittest
from pathlib import Path

from engine.paper_copy_runner import queue_paper_orders, reset_paper_simulator_for_tests
from engine.paper_simulator import PaperSimulator
from engine.pending_copy_report import attach_pending_copy


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def buy_candidate(signature="sig-pending-report-1"):
    return {
        "signature": signature,
        "status": "success",
        "metadata_complete": True,
        "signers": [WALLET],
        "signature_observation": {
            "confirmation_status": "finalized",
            "slot": 1,
            "block_time": 1700000000,
        },
        "trade_signal": {
            "classification": "BUY_CANDIDATE",
            "asset_mints": [ASSET],
            "quote_mints": [QUOTE],
        },
        "token_balance_changes": [
            {
                "owner": WALLET,
                "mint": ASSET,
                "decimals": 6,
                "delta_amount": "1000000",
                "pre_amount": "0",
                "post_amount": "1000000",
            },
            {
                "owner": WALLET,
                "mint": QUOTE,
                "decimals": 6,
                "delta_amount": "-5000000",
            },
        ],
    }


class PendingCopyReportTests(unittest.TestCase):
    def setUp(self):
        reset_paper_simulator_for_tests()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "paper.db"

    def tearDown(self):
        reset_paper_simulator_for_tests()
        self._tmpdir.cleanup()

    def test_attach_pending_copy_reports_queued_orders(self):
        simulator = PaperSimulator(db_path=self.db_path)
        simulator.add_virtual_balance("USD", "1000")
        queued = queue_paper_orders(WALLET, [buy_candidate()], simulator)
        self.assertEqual(len(queued), 1)

        report = {"mode": "PAPER_SIMULATION", "balances": {}}
        attach_pending_copy(report, simulator=simulator)

        self.assertIn("pending_copy", report)
        self.assertEqual(report["pending_copy"]["pending_count"], 1)
        self.assertEqual(len(report["pending_copy"]["pending_orders"]), 1)
        self.assertFalse(report["pending_copy"]["real_trading"])
        self.assertFalse(report["pending_copy"]["transaction_broadcast"])

    def test_attach_pending_copy_zero_when_empty(self):
        simulator = PaperSimulator(db_path=self.db_path)
        report = {}
        attach_pending_copy(report, simulator=simulator)
        self.assertEqual(report["pending_copy"]["pending_count"], 0)
        self.assertEqual(report["pending_copy"]["pending_orders"], [])


if __name__ == "__main__":
    unittest.main()
