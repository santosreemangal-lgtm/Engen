import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.monitor_service import MonitorService
from engine.signature_store import SignatureStore
from engine.solana_public import SolanaRPCError, SolanaRPCProviderError
from engine.transaction_monitor import process_new_signatures
from engine.wallet_monitor import get_and_store_new_wallet_signatures


WALLET = "11111111111111111111111111111111"
SIGNATURE = "1" * 64
DISCOVERY_WARNING = (
    "New signature discovery failed; saved pending signatures are still being processed."
)


class BacklogProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SignatureStore(
            db_path=Path(self.temp_dir.name) / "signatures.db"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_discovery_rpc_failure_does_not_block_saved_pending_work(self):
        self.store.mark_baseline(WALLET)
        self.store.save(WALLET, SIGNATURE, slot=123)

        with patch(
            "engine.wallet_monitor.get_wallet_signatures",
            side_effect=SolanaRPCError("temporary provider outage"),
        ):
            pending = get_and_store_new_wallet_signatures(
                WALLET, store=self.store, limit=10
            )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["signature"], SIGNATURE)
        self.assertEqual(pending[0]["discovery_warning"], DISCOVERY_WARNING)
        self.assertEqual(self.store.get_status(WALLET, SIGNATURE), "pending")

    def test_pagination_rpc_failure_still_drains_saved_queue(self):
        self.store.mark_baseline(WALLET)
        saved_pending = "1" * 64
        self.store.save(WALLET, saved_pending, slot=100)
        suffixes = "23456789ABCDEFGH"
        page_signatures = [
            {"signature": "1" * 62 + "9" + suffixes[index], "slot": 200 - index}
            for index in range(10)
        ]
        with patch(
            "engine.wallet_monitor.get_wallet_signatures",
            side_effect=[page_signatures, SolanaRPCError("pagination unavailable")],
        ):
            pending = get_and_store_new_wallet_signatures(
                WALLET, store=self.store, limit=10
            )

        returned = {item["signature"] for item in pending}
        self.assertEqual(len(pending), 11)
        self.assertIn(saved_pending, returned)
        self.assertTrue(
            all(item.get("discovery_warning") == DISCOVERY_WARNING for item in pending)
        )

    def test_discovery_failure_without_saved_backlog_still_surfaces_error(self):
        self.store.mark_baseline(WALLET)
        with patch(
            "engine.wallet_monitor.get_wallet_signatures",
            side_effect=SolanaRPCError("temporary provider outage"),
        ):
            with self.assertRaises(SolanaRPCError):
                get_and_store_new_wallet_signatures(
                    WALLET, store=self.store, limit=10
                )

    def test_provider_rpc_failures_are_retried_then_moved_to_manual_review(self):
        with patch(
            "engine.transaction_monitor.process_transaction_signature",
            side_effect=SolanaRPCProviderError(-32005),
        ):
            for attempt in range(1, 6):
                result = process_new_signatures(
                    [{"signature": SIGNATURE}],
                    wallet_address=WALLET,
                    store=self.store,
                )[0]
                error = self.store.get_error_record(WALLET, SIGNATURE)

                self.assertEqual(error["attempts"], attempt)
                if attempt < 5:
                    self.assertEqual(result["processing_status"], "pending")
                    self.assertTrue(result["retryable"])
                    self.assertEqual(self.store.get_status(WALLET, SIGNATURE), "pending")
                else:
                    self.assertEqual(result["processing_status"], "needs_review")
                    self.assertFalse(result["retryable"])
                    self.assertEqual(
                        self.store.get_status(WALLET, SIGNATURE), "needs_review"
                    )

        self.assertEqual(self.store.get_pending_signatures(WALLET), [])
        self.assertFalse(self.store.mark_processed(WALLET, SIGNATURE))
        self.assertEqual(self.store.get_status(WALLET, SIGNATURE), "needs_review")

    def test_discovery_warning_is_preserved_in_processed_result(self):
        with patch(
            "engine.transaction_monitor.process_transaction_signature",
            return_value={
                "signature": SIGNATURE,
                "available": True,
                "metadata_complete": True,
                "status": "success",
                "retryable": False,
            },
        ):
            results = process_new_signatures(
                [{
                    "signature": SIGNATURE,
                    "discovery_warning": DISCOVERY_WARNING,
                }]
            )
        self.assertEqual(results[0]["discovery_warning"], DISCOVERY_WARNING)

    def test_signature_statuses_are_read_in_one_bounded_batch(self):
        processed = "1" * 64
        review = "2" * 64
        self.store.save(WALLET, processed)
        self.store.save(WALLET, review)
        self.store.mark_processed(WALLET, processed)
        for _ in range(5):
            self.store.record_failure(
                WALLET, review, "rpc_fetch", "rpc_error", "provider unavailable"
            )

        self.assertEqual(
            self.store.get_statuses(WALLET, [processed, review, "3" * 64]),
            {processed: "processed", review: "needs_review"},
        )

    def test_backlog_stats_include_status_counts_age_and_retry_pressure(self):
        pending = "1" * 64
        needs_review = "2" * 64
        processed = "3" * 64

        self.store.save(WALLET, pending)
        self.store.save(WALLET, needs_review)
        self.store.save(WALLET, processed)
        self.store.mark_processed(WALLET, processed)

        for _ in range(5):
            self.store.record_failure(
                WALLET, needs_review, "rpc_fetch",
                "rpc_error", "temporary provider error",
            )
        for _ in range(3):
            self.store.record_failure(
                WALLET, pending, "rpc_fetch",
                "rpc_error", "temporary provider error",
            )

        stats = self.store.get_backlog_stats(WALLET)
        self.assertEqual(stats["total_count"], 3)
        self.assertEqual(stats["pending_count"], 1)
        self.assertEqual(stats["processed_count"], 1)
        self.assertEqual(stats["needs_review_count"], 1)
        self.assertEqual(stats["pending_retry_pressure_count"], 1)
        self.assertGreaterEqual(stats["pending_max_attempts"], 3)
        self.assertIsNotNone(stats["oldest_pending_at"])
        self.assertGreaterEqual(stats["oldest_pending_age_seconds"], 0)

    def test_monitor_status_exposes_backlog_metrics_and_scan_counts(self):
        pending = [8]
        scan_finished = threading.Event()

        def scan(_address):
            pending[0] = max(0, pending[0] - 2)
            scan_finished.set()
            return [
                {"signature": "one", "retryable": False},
                {"signature": "two", "retryable": True},
            ]

        scan.metrics_function = lambda _address: {
            "pending_count": pending[0],
            "needs_review_count": 1,
            "oldest_pending_age_seconds": 45,
        }
        service = MonitorService(scan, interval=30)
        try:
            service.start([WALLET])
            self.assertTrue(scan_finished.wait(timeout=3))
            status = service.stop_and_wait(timeout=3)
            wallet = status["wallets"][0]

            self.assertEqual(wallet["last_signatures_attempted_count"], 2)
            self.assertEqual(wallet["last_retry_count"], 1)
            self.assertEqual(wallet["backlog_metrics"]["pending_count"], 6)
            self.assertEqual(
                wallet["backlog_metrics"]["oldest_pending_age_seconds"], 45
            )
        finally:
            service.stop_and_wait(timeout=3)

    def test_backlog_net_drain_rate_is_signed_and_measured_per_minute(self):
        service = MonitorService(lambda _address: [], interval=30)
        service._backlog_samples[WALLET] = (10.0, 10)

        with patch("dashboard.monitor_service.time.monotonic", return_value=70.0):
            delta, rate = service._sample_backlog(
                WALLET, {"pending_count": 7}
            )
        self.assertEqual(delta, 3)
        self.assertEqual(rate, 3.0)

        with patch("dashboard.monitor_service.time.monotonic", return_value=130.0):
            delta, rate = service._sample_backlog(
                WALLET, {"pending_count": 9}
            )
        self.assertEqual(delta, -2)
        self.assertEqual(rate, -2.0)


    def test_fresh_queue_precedes_history_without_downgrading_excess_fresh_rows(self):
        now = 1_800_000_000
        fresh_signatures = []
        history_signatures = []
        for index in range(55):
            signature = (format(index + 1, "064x"))
            fresh_signatures.append(signature)
            self.store.save(
                WALLET, signature, slot=2000 + index,
                block_time=now - index - 1,
            )
        for index in range(40):
            signature = format(1000 + index, "064x")
            history_signatures.append(signature)
            self.store.save(
                WALLET, signature, slot=1000 + index,
                block_time=now - 100 - index,
            )

        first_fresh = self.store.get_fresh_pending_signatures(
            WALLET, limit=50, max_age_seconds=60,
            future_skew_seconds=5, now=now,
        )
        history = self.store.get_history_pending_signatures(
            WALLET, limit=25, history_cap=1000,
            exclude_signatures=[item["signature"] for item in first_fresh],
            before_block_time=now - 60,
        )
        remaining_fresh = self.store.get_fresh_pending_signatures(
            WALLET, limit=50, max_age_seconds=60,
            future_skew_seconds=5, now=now,
        )

        self.assertEqual(len(first_fresh), 50)
        self.assertEqual(len(history), 25)
        self.assertTrue(set(history).issubset(set(history_signatures)))
        self.assertEqual(
            set(item["signature"] for item in remaining_fresh),
            set(fresh_signatures) - {item["signature"] for item in first_fresh},
        )

    def test_recoverable_needs_review_is_requeued_once_inside_recent_window(self):
        signature = "7" * 64
        self.store.save(WALLET, signature)
        for _ in range(5):
            self.store.record_failure(
                WALLET, signature, "rpc_fetch", "rpc_error",
                "legacy generic RPC failure",
            )
        with self.store._connect() as db:
            db.execute(
                "UPDATE seen_signatures SET last_checked_at=datetime('now','-60 seconds') "
                "WHERE wallet_address=? AND signature=?",
                (WALLET, signature),
            )

        requeued = self.store.requeue_recoverable_needs_review(
            WALLET, limit=10, cooldown_seconds=30, history_cap=1000,
        )
        self.assertEqual(requeued, [signature])
        self.assertEqual(self.store.get_status(WALLET, signature), "pending")
        with self.store._connect() as db:
            count = db.execute(
                "SELECT requeue_count FROM seen_signatures "
                "WHERE wallet_address=? AND signature=?",
                (WALLET, signature),
            ).fetchone()[0]
            db.execute(
                "UPDATE seen_signatures SET last_checked_at=datetime('now','-60 seconds') "
                "WHERE wallet_address=? AND signature=?",
                (WALLET, signature),
            )
        self.assertEqual(count, 1)
        self.assertEqual(
            self.store.requeue_recoverable_needs_review(
                WALLET, limit=10, cooldown_seconds=30, history_cap=1000,
            ),
            [],
        )

if __name__ == "__main__":
    unittest.main()
