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

    def test_incomplete_pagination_does_not_advance_processed_checkpoint(self):
        store = Mock()
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        results = [
            candidate_result("UNKNOWN"),
            {
                "scan_incomplete": True,
                "pagination_mode": "catchup",
                "pagination_pages_scanned": 101,
                "pagination_error": "Checkpoint not reached.",
            },
        ]
        with (
            patch("dashboard.monitor_bridge.SignatureStore", return_value=store),
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=results),
            patch("dashboard.monitor_bridge.queue_paper_orders", return_value=[]),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: simulator,
            )
            scan(WALLET)

        store.mark_processed.assert_called_once_with(
            WALLET, "synthetic-signature-for-bridge-test", advance_position=False
        )

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


    def _persisted_history_kwargs(self, result, queued_orders=None):
        import uuid

        signature_store = Mock()
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        profile_store = Mock()
        signature = "1" * 64
        result = {**result, "signature": signature}
        with (
            patch("dashboard.monitor_bridge.SignatureStore", return_value=signature_store),
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=[result]),
            patch(
                "dashboard.monitor_bridge.queue_paper_orders",
                return_value=queued_orders or [],
            ),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: simulator,
                profile_id=str(uuid.uuid4()),
                profile_store=profile_store,
            )
            scan(WALLET)
        self.assertTrue(profile_store.save_transaction.called)
        return profile_store.save_transaction.call_args.kwargs

    def test_history_amount_aggregates_only_verified_asset_rows(self):
        result = {
            "status": "success",
            "metadata_complete": True,
            "retryable": False,
            "signers": [WALLET],
            "signature_observation": {"confirmation_status": "finalized"},
            "trade_signal": {
                "classification": "BUY_CANDIDATE",
                "asset_mints": [ASSET],
                "quote_mints": [QUOTE],
            },
            "token_balance_changes": [
                {"owner": WALLET, "mint": ASSET, "decimals": 6, "delta_amount": "100"},
                {"owner": WALLET, "mint": ASSET, "decimals": 6, "delta_amount": "250"},
                {"owner": WALLET, "mint": QUOTE, "decimals": 6, "delta_amount": "-500"},
                {"owner": "ANOTHER_WALLET", "mint": ASSET, "decimals": 6, "delta_amount": "999"},
            ],
        }
        kwargs = self._persisted_history_kwargs(
            result,
            queued_orders=[{"signature": "1" * 64, "order_id": "paper-order-1", "side": "BUY"}],
        )
        self.assertEqual(kwargs["token_mint"], ASSET)
        self.assertEqual(kwargs["token_decimals"], 6)
        self.assertEqual(kwargs["raw_amount"], "350")
        self.assertEqual(
            kwargs["evidence"]["token_amount_attribution"],
            "aggregated_wallet_owned_asset_rows",
        )

    def test_unknown_activity_never_gets_an_arbitrary_token_amount(self):
        result = {
            "status": "success",
            "metadata_complete": True,
            "retryable": False,
            "signers": [WALLET],
            "signature_observation": {"confirmation_status": "finalized"},
            "trade_signal": {
                "classification": "UNKNOWN",
                "asset_mints": [],
                "quote_mints": [],
                "reason": "Could not prove a swap direction.",
            },
            "token_balance_changes": [
                {"owner": WALLET, "mint": ASSET, "decimals": 6, "delta_amount": "100"},
                {"owner": WALLET, "mint": QUOTE, "decimals": 6, "delta_amount": "-500"},
            ],
        }
        kwargs = self._persisted_history_kwargs(result)
        self.assertIsNone(kwargs["token_mint"])
        self.assertIsNone(kwargs["token_decimals"])
        self.assertIsNone(kwargs["raw_amount"])
        self.assertEqual(
            kwargs["evidence"]["token_amount_attribution"],
            "unavailable_not_verified_swap",
        )

    def test_unconfirmed_candidate_never_gets_a_history_token_amount(self):
        result = {
            "status": "success",
            "metadata_complete": False,
            "retryable": True,
            "signers": [WALLET],
            "signature_observation": {"confirmation_status": "processed"},
            "trade_signal": {
                "classification": "BUY_CANDIDATE",
                "asset_mints": [ASSET],
                "quote_mints": [QUOTE],
            },
            "token_balance_changes": [
                {"owner": WALLET, "mint": ASSET, "decimals": 6, "delta_amount": "100"},
            ],
        }
        kwargs = self._persisted_history_kwargs(result)
        self.assertIsNone(kwargs["token_mint"])
        self.assertIsNone(kwargs["raw_amount"])
        self.assertFalse(kwargs["evidence"]["verified_swap_for_paper_queue"])


if __name__ == "__main__":
    unittest.main()
