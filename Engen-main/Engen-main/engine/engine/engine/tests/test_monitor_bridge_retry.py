import unittest
import time
from unittest.mock import Mock, patch

from dashboard.monitor_bridge import create_scan_function, _map_profile_transaction_status


WALLET = "11111111111111111111111111111111"
ASSET = "TokenMint1111111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def candidate_result(classification="BUY_CANDIDATE"):
    return {
        "signature": "1" * 64,
        "status": "success",
        "metadata_complete": True,
        "retryable": False,
        "signers": [WALLET],
        "signature_observation": {"confirmation_status": "finalized", "slot": 123, "block_time": int(time.time())},
        "trade_signal": {
            "classification": classification,
            "asset_mints": [ASSET] if classification.endswith("_CANDIDATE") else [],
            "quote_mints": [QUOTE] if classification.endswith("_CANDIDATE") else [],
        },
        "token_balance_changes": [],
    }


class MonitorBridgeRetryTests(unittest.TestCase):
    def test_profile_status_mapping_covers_pending_confirmed_failed_unknown(self):
        cases = [
            (
                "pending",
                {
                    "status": "success", "metadata_complete": False,
                    "retryable": True,
                    "signature_observation": {"confirmation_status": "processed"},
                },
            ),
            (
                "confirmed",
                {
                    "status": "success", "metadata_complete": True,
                    "retryable": False,
                    "signature_observation": {"confirmation_status": "finalized"},
                },
            ),
            (
                "failed",
                {
                    "status": "failed", "metadata_complete": True,
                    "retryable": False,
                    "signature_observation": {"confirmation_status": "finalized"},
                },
            ),
            (
                "failed",
                {
                    "status": "success", "metadata_complete": True,
                    "retryable": True,
                    "signature_observation": {
                        "confirmation_status": "confirmed",
                        "signature_error": {"InstructionError": [0, "Custom"]},
                    },
                },
            ),
            (
                "unknown",
                {
                    "status": "success", "metadata_complete": False,
                    "retryable": False, "processing_status": "needs_review",
                    "signature_observation": {"confirmation_status": "confirmed"},
                },
            ),
            (
                "unknown",
                {
                    "status": "success", "metadata_complete": True,
                    "retryable": False,
                    "signature_observation": {"confirmation_status": None},
                },
            ),
        ]
        for expected, result in cases:
            with self.subTest(expected=expected, result=result):
                self.assertEqual(_map_profile_transaction_status(result), expected)

    def test_bridge_persists_pending_profile_status_for_retryable_metadata(self):
        result = candidate_result()
        result["metadata_complete"] = False
        result["retryable"] = True
        result["signature_observation"]["confirmation_status"] = "processed"
        profile_store = Mock()

        store, _, _ = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
            signature_status="pending",
        )

        self.assertEqual(profile_store.save_transaction.call_args.kwargs["status"], "pending")
        self.assertEqual(
            profile_store.save_transaction.call_args.kwargs["evidence"]["signature_queue_status"],
            "pending",
        )
        store.mark_processed.assert_not_called()

    def test_bridge_persists_confirmed_transaction_without_confusing_queue_pending(self):
        result = candidate_result()
        profile_store = Mock()

        store, _, _ = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
            signature_status="pending",
        )

        saved = profile_store.save_transaction.call_args.kwargs
        self.assertEqual(saved["status"], "confirmed")
        self.assertEqual(saved["evidence"]["signature_queue_status"], "pending")
        store.mark_processed.assert_not_called()

    def test_bridge_persists_on_chain_failure_as_failed_and_finishes_queue(self):
        result = candidate_result()
        result["status"] = "failed"
        result["signature_observation"]["signature_error"] = {
            "InstructionError": [0, "Custom"]
        }
        profile_store = Mock()

        store, _, _ = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
            signature_status="processed",
        )

        saved = profile_store.save_transaction.call_args.kwargs
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["evidence"]["signature_queue_status"], "processed")
        store.mark_processed.assert_called_once_with(WALLET, "1" * 64)

    def test_needs_review_profile_evidence_tracks_signature_queue_status(self):
        result = candidate_result()
        result["metadata_complete"] = False
        result["retryable"] = False
        result["processing_status"] = "needs_review"
        profile_store = Mock()

        store, _, _ = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
        )

        evidence = profile_store.save_transaction.call_args.kwargs["evidence"]
        self.assertEqual(evidence["signature_queue_status"], "needs_review")
        self.assertTrue(evidence["retry_exhausted"])
        store.mark_processed.assert_not_called()

    def _scan_with(self, result, queued_orders, *, profile_store=None, profile_id=None,
                   simulator_orders=None, signature_status=None, error_record=None):
        store = Mock()
        store.get_status.return_value = signature_status
        store.get_statuses.return_value = (
            {result.get("signature"): signature_status}
            if signature_status is not None else {}
        )
        store.get_error_record.return_value = error_record or {}
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        simulator.report.return_value = {"orders": simulator_orders or []}
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
                profile_id=profile_id,
                profile_store=profile_store,
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
            WALLET, "1" * 64
        )
        self.assertNotIn("paper_copy_status", output[0])


    def test_stale_candidate_is_skipped_and_not_left_in_retry_queue(self):
        result = candidate_result()
        result["signature_observation"]["block_time"] = int(time.time()) - 61
        store, _, output = self._scan_with(result, [])

        store.mark_processed.assert_called_once_with(WALLET, "1" * 64)
        self.assertEqual(output[0]["paper_copy_status"], "SKIPPED_STALE_SOURCE")
        self.assertNotIn("RETRYING_QUEUE", output[0].get("paper_copy_status", ""))

    def test_stale_candidate_is_terminally_skipped_even_when_queue_raises(self):
        result = candidate_result()
        result["signature_observation"]["block_time"] = int(time.time()) - 61
        store = Mock()
        simulator = Mock()
        simulator.report.return_value = {"orders": []}
        with (
            patch("dashboard.monitor_bridge.SignatureStore", return_value=store),
            patch("dashboard.monitor_bridge.monitor_wallet", return_value=[result]),
            patch(
                "dashboard.monitor_bridge.queue_paper_orders",
                side_effect=RuntimeError("temporary queue failure"),
            ),
            patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
        ):
            scan = create_scan_function(
                side_getter=lambda: "BOTH",
                settings_getter=lambda: {},
                simulator_getter=lambda: simulator,
            )
            output = scan(WALLET)

        store.mark_processed.assert_called_once_with(WALLET, "1" * 64)
        self.assertEqual(output[0]["paper_copy_status"], "SKIPPED_STALE_SOURCE")
        self.assertIn("paper_copy_error", output[0])

    def test_missing_source_timestamp_is_skipped_and_marked_processed(self):
        result = candidate_result()
        result["signature_observation"].pop("block_time")

        store, _, output = self._scan_with(result, [])

        store.mark_processed.assert_called_once_with(WALLET, "1" * 64)
        self.assertEqual(output[0]["paper_copy_status"], "SKIPPED_STALE_SOURCE")
        self.assertIn("missing", output[0]["paper_copy_message"].lower())
        self.assertNotIn("RETRYING_QUEUE", output[0]["paper_copy_status"])

    def test_needs_review_is_terminal_unknown_profile_status_not_pending(self):
        result = candidate_result()
        result["metadata_complete"] = False
        result["processing_status"] = "needs_review"
        profile_store = Mock()

        store, _, output = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
        )

        self.assertEqual(profile_store.save_transaction.call_args.kwargs["status"], "unknown")
        store.mark_processed.assert_not_called()
        self.assertEqual(output[0]["paper_copy_status"], "NEEDS_REVIEW")

    def test_reconciles_legacy_profile_pending_rows_to_terminal_review(self):
        result = candidate_result()
        result["signature"] = "2" * 64
        profile_store = Mock()
        profile_store.get_pending_transaction_signatures.return_value = [
            result["signature"]
        ]
        store, _, _ = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
            signature_status="needs_review",
            error_record={
                "attempts": 5, "error_stage": "rpc_fetch",
                "error_code": "rpc_error", "error_message": "provider unavailable",
            },
        )
        store.get_statuses.assert_called_once_with(
            WALLET, [result["signature"]]
        )
        store.get_status.assert_not_called()
        profile_store.get_pending_transaction_signatures.assert_called_once_with(
            WALLET, limit=1000
        )
        profile_store.mark_transaction_needs_review.assert_called_once_with(
            result["signature"], WALLET,
            attempts=5, error_stage="rpc_fetch", error_code="rpc_error",
            error_message="provider unavailable",
        )

    def test_reconciles_paper_orders_to_durable_source_trade_refs(self):
        profile_store = Mock()
        order = {
            "order_id": "paper-order-1",
            "side": "BUY",
            "asset_mint": ASSET,
            "details": {
                "origin": "AUTOMATIC_WALLET_COPY",
                "source_wallet": WALLET,
                "source_signature": "1" * 64,
                "source_slot": 123,
                "source_block_time": int(time.time()),
            },
        }
        queued = [{"signature": "1" * 64, "order_id": "paper-order-1", "side": "BUY"}]

        self._scan_with(
            candidate_result(), queued, profile_store=profile_store,
            profile_id="profile-test", simulator_orders=[order],
        )

        profile_store.save_source_trade_ref.assert_called_once_with(
            "profile-test", "paper-order-1", WALLET, "1" * 64, "BUY", ASSET,
            source_slot=123, source_block_time=order["details"]["source_block_time"],
        )


if __name__ == "__main__":
    unittest.main()
