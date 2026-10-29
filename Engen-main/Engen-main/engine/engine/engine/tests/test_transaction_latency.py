import unittest
from unittest.mock import patch

from engine.latency import LatencyRecorder
from engine.transaction_processor import process_transaction_signature


class TransactionLatencyTests(unittest.TestCase):
    @patch("engine.transaction_processor.get_transaction")
    def test_processor_records_rpc_and_analysis_stages(self, mock_get):
        mock_get.return_value = {
            "slot": 123,
            "transaction": {"message": {"accountKeys": ["wallet_A"]}},
            "meta": {
                "err": None,
                "fee": 5000,
                "preTokenBalances": [],
                "postTokenBalances": [],
                "preBalances": [100],
                "postBalances": [100],
            },
        }
        recorder = LatencyRecorder()

        result = process_transaction_signature(
            "1111111111111111111111111111111111111111111111111111111111111112",
            latency_recorder=recorder,
        )

        self.assertTrue(result["available"])
        summary = recorder.summary()
        self.assertEqual(summary["transaction.rpc_fetch"]["count"], 1)
        self.assertEqual(summary["transaction.local_analysis"]["count"], 1)
        self.assertGreaterEqual(summary["transaction.rpc_fetch"]["max_ms"], 0)
        self.assertGreaterEqual(summary["transaction.local_analysis"]["max_ms"], 0)

    @patch("engine.transaction_processor.get_transaction", return_value=None)
    def test_unavailable_transaction_still_records_rpc_stage(self, mock_get):
        recorder = LatencyRecorder()

        result = process_transaction_signature(
            "1111111111111111111111111111111111111111111111111111111111111113",
            latency_recorder=recorder,
        )

        self.assertFalse(result["available"])
        summary = recorder.summary()
        self.assertIn("transaction.rpc_fetch", summary)
        self.assertNotIn("transaction.local_analysis", summary)


if __name__ == "__main__":
    unittest.main()
