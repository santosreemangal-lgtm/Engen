import json
import unittest
from unittest.mock import patch

from engine.latency import LatencyRecorder
from engine.transaction_monitor import process_new_signatures
from engine.wallet_monitor import get_wallet_signatures


class MonitorLatencyTests(unittest.TestCase):
    @patch("engine.rpc_policy.urllib.request.urlopen")
    def test_signature_fetch_records_rpc_latency(self, mock_urlopen):
        response = mock_urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": [{"signature": "1111111111111111111111111111111111111111111111111111111111111112"}],
        }).encode("utf-8")
        recorder = LatencyRecorder()

        result = get_wallet_signatures(
            "11111111111111111111111111111111",
            latency_recorder=recorder,
        )

        self.assertEqual(result, [{"signature": "1111111111111111111111111111111111111111111111111111111111111112"}])
        summary = recorder.summary()
        self.assertEqual(summary["wallet.signature_fetch"]["count"], 1)

    @patch("engine.transaction_monitor.process_transaction_signature")
    def test_monitor_passes_recorder_to_transaction_processor(self, mock_process):
        mock_process.return_value = {
            "signature": "1111111111111111111111111111111111111111111111111111111111111112",
            "available": True,
        }
        recorder = LatencyRecorder()

        results = process_new_signatures(
            [{"signature": "1111111111111111111111111111111111111111111111111111111111111112"}],
            latency_recorder=recorder,
        )

        self.assertEqual(len(results), 1)
        self.assertIs(
            mock_process.call_args.kwargs["latency_recorder"],
            recorder,
        )


if __name__ == "__main__":
    unittest.main()
