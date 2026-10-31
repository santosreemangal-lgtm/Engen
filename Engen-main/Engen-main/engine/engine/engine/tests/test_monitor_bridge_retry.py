import unittest
from unittest.mock import Mock, patch

from dashboard.monitor_bridge import create_scan_function


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def candidate_result(classification="BUY_CANDIDATE"):
    return {
        "signature": "synthetic-signature-for-bridge-test",
        "status": "success",
        "metadata_complete": True,
        "retryable": False,
        "signers": [WALLET],
        "signature_observation": {"confirmation_status": "finalized"},
        "trade_signal": {
            "classification": classification,
            "asset_mints": [ASSET] if classification.endswith("_CANDIDATE") else [],
            "quote_mints": [QUOTE] if classification.endswith("_CANDIDATE") else [],
        },
        "token_balance_changes": [],
    }


class MonitorBridgeRetryTests(unittest.TestCase):
    def _scan_with(self, result, queued_orders):
        store = Mock()
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        with (
            patch("dashboard.monitor_bridge.SignatureStore", return_value=store),
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=[result]),
            patch("dashboard.monitor_bridge.queue_paper_orders", return_value=queued_orders),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: simulator,
            )
            output = scan(WALLET)
        return store, simulator, output

    def test_candidate_that_did_not_queue_remains_pending_for_retry(self):
        store, _, output = self._scan_with(candidate_result(), [])

        store.mark_processed.assert_not_called()
        self.assertEqual(output[0]["paper_copy_status"], "RETRYING_QUEUE")
        self.assertIn("remains pending", output[0]["paper_copy_message"])

    def test_non_candidate_is_marked_processed_to_avoid_retry_loop(self):
        store, _, output = self._scan_with(candidate_result("UNKNOWN"), [])

        store.mark_processed.assert_called_once_with(
            WALLET, "synthetic-signature-for-bridge-test"
        )
        self.assertNotIn("paper_copy_status", output[0])

    def test_needs_review_rpc_error_is_persisted_without_being_marked_processed(self):
        from uuid import uuid4

        profile_store = Mock()
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        signature = "1" * 63 + "2"
        result = {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "rpc_error",
            "processing_status": "needs_review",
            "error": "Public Solana RPC request failed.",
            "retryable": False,
        }
        with (
            patch("dashboard.monitor_bridge.SignatureStore") as store_class,
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=[result]),
            patch("dashboard.monitor_bridge.queue_paper_orders", return_value=[]),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            store = store_class.return_value
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: simulator,
                profile_id=str(uuid4()),
                profile_store=profile_store,
            )
            output = scan(WALLET)

        self.assertEqual(output[0]["profile_history_status"], "saved")
        self.assertEqual(profile_store.save_transaction.call_args.kwargs["status"], "needs_review")
        self.assertEqual(
            profile_store.save_transaction.call_args.kwargs["evidence"]["processing_status"],
            "needs_review",
        )
        store.mark_processed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
