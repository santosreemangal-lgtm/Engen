import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engine.latency import LatencyRecorder
from engine.paper_monitor_runner import run_once


class PaperLatencyTests(unittest.TestCase):
    @patch("engine.paper_monitor_runner.monitor_wallet")
    def test_run_once_records_monitor_and_paper_decision(self, mock_monitor):
        mock_monitor.return_value = [{
            "signature": "paper-signature",
            "available": True,
            "status": "success",
        }]
        args = SimpleNamespace(
            wallet="PublicWalletAddress",
            limit=5,
            rpc_url="https://example.invalid",
        )
        store = Mock()
        trader = Mock()
        trader.process.return_value = {"status": "PAPER_FILLED"}
        trader.get_balances.return_value = {"QUOTE": "100"}
        trader.trades = []
        recorder = LatencyRecorder()

        with redirect_stdout(io.StringIO()):
            run_once(args, trader, store, latency_recorder=recorder)

        summary = recorder.summary()
        self.assertEqual(summary["paper.monitor_scan"]["count"], 1)
        self.assertEqual(summary["paper.local_decision"]["count"], 1)
        self.assertIs(
            mock_monitor.call_args.kwargs["latency_recorder"],
            recorder,
        )
        store.mark_processed.assert_called_once_with(
            "PublicWalletAddress",
            "paper-signature",
        )


if __name__ == "__main__":
    unittest.main()
