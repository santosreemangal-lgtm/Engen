import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.signature_store import SignatureStore


WALLET = "11111111111111111111111111111111"
SIGNATURE = "1" * 63 + "2"


class SignatureRetryBackoffTests(unittest.TestCase):
    def test_new_pending_signature_is_immediately_eligible(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SignatureStore(Path(temp) / "signatures.db")
            store.save(WALLET, SIGNATURE)
            self.assertEqual(store.get_pending_signatures(WALLET), [SIGNATURE])

    def test_retry_backoff_delays_repeated_rpc_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "signatures.db"
            store = SignatureStore(path)
            store.save(WALLET, SIGNATURE)
            failure = store.record_failure(
                WALLET, SIGNATURE, "rpc_fetch",
                "rpc_error", "Provider temporarily unavailable."
            )
            self.assertEqual(failure, {"status": "pending", "attempts": 1})
            self.assertEqual(store.get_pending_signatures(WALLET), [])

            with sqlite3.connect(path) as db:
                db.execute(
                    """UPDATE seen_signatures
                       SET last_checked_at=datetime('now','-16 seconds')
                       WHERE wallet_address=? AND signature=?""",
                    (WALLET, SIGNATURE),
                )
            self.assertEqual(store.get_pending_signatures(WALLET), [SIGNATURE])

    def test_needs_review_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SignatureStore(Path(temp) / "signatures.db")
            store.save(WALLET, SIGNATURE)
            for _ in range(5):
                result = store.record_failure(
                    WALLET, SIGNATURE, "rpc_fetch",
                    "rpc_error", "Provider temporarily unavailable."
                )
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(store.get_pending_signatures(WALLET), [])


if __name__ == "__main__":
    unittest.main()
