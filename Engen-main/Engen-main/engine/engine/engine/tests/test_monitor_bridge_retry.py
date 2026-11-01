import unittest
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard.monitor_bridge import create_scan_function
from engine.profile_store import ProfileStore
from engine.signature_store import SignatureStore


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
    def _scan_with(self, result, queued_orders, *, profile_store=None, profile_id=None, simulator_orders=None):
        store = Mock()
        simulator = Mock()
        simulator.orders = {}
        simulator.processed_signals = set()
        simulator.report.return_value = {"orders": simulator_orders or []}
        if profile_store is not None:
            profile_store.list_pending_transaction_signatures.return_value = []
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

    def test_needs_review_is_terminal_unknown_profile_status_not_pending(self):
        result = candidate_result()
        result["metadata_complete"] = False
        result["processing_status"] = "needs_review"
        profile_store = Mock()

        store, _, output = self._scan_with(
            result, [], profile_store=profile_store, profile_id="profile-test",
        )

        self.assertEqual(profile_store.save_transaction.call_args.kwargs["status"], "unknown")
        self.assertTrue(profile_store.save_transaction.call_args.kwargs["terminal_status"])
        # Terminal review rows are not retried, but their diagnostic state must
        # remain in signatures.db rather than being cleared by mark_processed().
        store.mark_processed.assert_not_called()
        self.assertEqual(output[0]["paper_copy_status"], "NEEDS_REVIEW")

    def test_reconciles_existing_pending_rows_against_needs_review_signature_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_store = ProfileStore(root / "profiles.db")
            profile_id = profile_store.create_profile("Legacy pending review")
            profile_store.associate_source_wallet(profile_id, WALLET)
            signature = "4" * 64
            profile_store.save_transaction(
                signature, WALLET, classification="BUY_CANDIDATE",
                status="pending", evidence={"old_status": "pending"},
                profile_ids=(profile_id,),
            )

            signature_store = SignatureStore(root / "signatures.db")
            signature_store.save(WALLET, signature)
            for attempt in range(5):
                state = signature_store.record_failure(
                    WALLET, signature, "rpc_fetch", "rpc_error",
                    "public RPC request failed",
                )
            self.assertEqual(state["status"], "needs_review")

            simulator = Mock()
            simulator.orders = {}
            simulator.processed_signals = set()
            simulator.report.return_value = {"orders": []}
            with (
                patch("dashboard.monitor_bridge.SignatureStore", return_value=signature_store),
                patch("dashboard.monitor_bridge.monitor_wallet", return_value=[]),
                patch("dashboard.monitor_bridge.queue_paper_orders", return_value=[]),
                patch("dashboard.monitor_bridge.execute_pending_paper_orders", return_value=[]),
            ):
                scan = create_scan_function(
                    side_getter=lambda: "BOTH",
                    settings_getter=lambda: {},
                    simulator_getter=lambda: simulator,
                    profile_id=profile_id,
                    profile_store=profile_store,
                )
                self.assertEqual(scan(WALLET), [])

            stored = profile_store.get_transaction(signature, profile_id)
            self.assertEqual(stored["status"], "unknown")
            self.assertEqual(stored["evidence"]["processing_status"], "needs_review")
            self.assertEqual(stored["evidence"]["retry_attempts"], 5)
            self.assertEqual(stored["evidence"]["retry_error_stage"], "rpc_fetch")
            self.assertEqual(stored["evidence"]["retry_error_code"], "rpc_error")
            # Reconciliation must not erase the original retry diagnostics.
            error = signature_store.get_error_record(WALLET, signature)
            self.assertEqual(error["status"], "needs_review")
            self.assertEqual(error["attempts"], 5)
            self.assertEqual(error["error_message"], "public RPC request failed")

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
