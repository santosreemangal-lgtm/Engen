import unittest
from unittest.mock import Mock, patch

from dashboard.monitor_bridge import create_scan_function
from engine.paper_copy_runner import queue_paper_orders


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

    def test_bridge_prefetches_market_data_outside_global_simulator_lock_and_records_latency(self):
        from engine.paper_copy_runner import PAPER_SIMULATOR_LOCK

        simulator = Mock()
        simulator.orders = {
            "paper-order-1": {
                "order_id": "paper-order-1",
                "side": "BUY",
                "asset_mint": ASSET,
                "quote_mint": "USD",
                "status": "CREATED",
                "details": {
                    "origin": "AUTOMATIC_WALLET_COPY",
                    "source_wallet": WALLET,
                    "target_notional_usd": "10",
                },
            }
        }
        simulator.processed_signals = set()
        provider_calls = []

        def price_provider(mint):
            acquired = PAPER_SIMULATOR_LOCK.acquire(blocking=False)
            self.assertTrue(acquired, "Public market-data fetch must not hold the simulator lock.")
            PAPER_SIMULATOR_LOCK.release()
            provider_calls.append(mint)
            return None

        with (
            patch("dashboard.monitor_bridge.SignatureStore", return_value=Mock()),
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=[]),
            patch("dashboard.monitor_bridge.queue_paper_orders", return_value=[]),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: simulator,
                price_provider=price_provider,
            )
            scan(WALLET)

        self.assertEqual(provider_calls, [ASSET])
        metrics = scan.latency_recorder.summary()
        for stage in (
            "dashboard.wallet_monitor_and_transaction_processing",
            "dashboard.transaction_history_persist",
            "dashboard.paper_order_queue",
            "dashboard.paper_market_data_fetch",
            "dashboard.paper_simulation",
            "dashboard.paper_lock_wait",
            "dashboard.wallet_scan_total",
        ):
            self.assertIn(stage, metrics)

    def test_candidate_that_did_not_queue_remains_pending_for_retry(self):
        store, _, output = self._scan_with(candidate_result(), [])

        store.mark_processed.assert_not_called()
        self.assertEqual(output[0]["paper_copy_status"], "RETRYING_QUEUE")
        self.assertIn("remains pending", output[0]["paper_copy_message"])

    def test_needs_review_signature_is_not_marked_processed(self):
        result = {
            **candidate_result("UNKNOWN"),
            "metadata_complete": False,
            "retryable": False,
            "processing_status": "needs_review",
        }
        store, _, _ = self._scan_with(result, [])

        store.mark_processed.assert_not_called()

    def test_historical_candidate_is_marked_processed_without_queue_retry(self):
        result = {**candidate_result(), "historical_baseline": True}
        store, _, output = self._scan_with(result, [])

        store.mark_processed.assert_called_once_with(
            WALLET, "synthetic-signature-for-bridge-test"
        )
        self.assertNotIn("paper_copy_status", output[0])

    def test_paper_queue_rejects_historical_baseline_candidates(self):
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        result = {**candidate_result(), "historical_baseline": True}

        queued = queue_paper_orders(WALLET, [result], simulator=simulator)

        self.assertEqual(queued, [])

    def test_non_candidate_is_marked_processed_to_avoid_retry_loop(self):
        store, _, output = self._scan_with(candidate_result("UNKNOWN"), [])

        store.mark_processed.assert_called_once_with(
            WALLET, "synthetic-signature-for-bridge-test"
        )
        self.assertNotIn("paper_copy_status", output[0])


if __name__ == "__main__":
    unittest.main()
