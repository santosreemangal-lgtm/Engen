
import json
import sqlite3
from contextlib import closing
from pathlib import Path


DEFAULT_DB_PATH = Path("data/signatures.db")
MAX_RAW_TRANSACTION_RECORDS_PER_WALLET = 1000


class SignatureStore:
    """Persist wallet signatures and processing status in SQLite."""

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _initialize(self):
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seen_signatures (
                        wallet_address TEXT NOT NULL,
                        signature TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (wallet_address, signature)
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wallet_baselines (
                        wallet_address TEXT PRIMARY KEY,
                        initialized_at TEXT NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(seen_signatures)"
                    ).fetchall()
                }
                # The original legacy schema had no processing status. Keep its
                # historical default behavior; replay only databases that already
                # tracked processed signatures before the historical-baseline fix.
                had_processing_status = "status" in columns

                migrations = {
                    "status": "TEXT NOT NULL DEFAULT 'processed'",
                    "attempts": "INTEGER NOT NULL DEFAULT 0",
                    "slot": "INTEGER",
                    "last_checked_at": "TEXT",
                    "error_stage": "TEXT",
                    "error_code": "TEXT",
                    "error_message": "TEXT",
                    "historical_baseline": "INTEGER NOT NULL DEFAULT 0",
                }
                for column, definition in migrations.items():
                    if column not in columns:
                        connection.execute(
                            "ALTER TABLE seen_signatures ADD COLUMN "
                            + column + " " + definition
                        )

                connection.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_positions (
                        wallet_address TEXT PRIMARY KEY,
                        slot INTEGER,
                        signature TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                connection.execute("""
                    CREATE TABLE IF NOT EXISTS transaction_records (
                        wallet_address TEXT NOT NULL,
                        signature TEXT NOT NULL,
                        raw_transaction_json TEXT,
                        processing_status TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (wallet_address, signature)
                    )
                """)

                # Older versions marked the initial wallet baseline processed
                # without fetching or persisting its transaction payloads.
                # Re-queue those entries exactly once for historical analysis;
                # the durable flag keeps them excluded from paper-copy orders.
                if had_processing_status:
                    connection.execute("""
                        UPDATE seen_signatures
                        SET historical_baseline = 1,
                            status = 'pending',
                            last_checked_at = NULL,
                            error_stage = NULL,
                            error_code = NULL,
                            error_message = NULL
                        WHERE status = 'processed'
                          AND historical_baseline = 0
                          AND wallet_address IN (
                              SELECT wallet_address FROM wallet_baselines
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM transaction_records tr
                              WHERE tr.wallet_address = seen_signatures.wallet_address
                                AND tr.signature = seen_signatures.signature
                          )
                    """)
                pruned = self._prune_transaction_records(
                    connection, limit=MAX_RAW_TRANSACTION_RECORDS_PER_WALLET
                )
        if pruned:
            # Keep all compact signature checkpoints for deduplication, but reclaim
            # the large raw-transaction payload pages removed by the retention rule.
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("VACUUM")

    @staticmethod
    def _prune_transaction_records(connection, wallet_address=None, limit=MAX_RAW_TRANSACTION_RECORDS_PER_WALLET):
        """Keep only the newest raw transaction payloads per wallet.

        seen_signatures is deliberately never pruned: its compact checkpoints
        prevent old transactions from being fetched and processed repeatedly.
        """
        if wallet_address is None:
            counts = connection.execute(
                "SELECT wallet_address, COUNT(*) FROM transaction_records GROUP BY wallet_address"
            ).fetchall()
            pruned = 0
            for address, count in counts:
                if count > limit:
                    pruned += SignatureStore._prune_transaction_records(
                        connection, address, limit
                    )
            return pruned
        count = connection.execute(
            "SELECT COUNT(*) FROM transaction_records WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()[0]
        if count <= limit:
            return 0
        cursor = connection.execute(
            """DELETE FROM transaction_records
               WHERE rowid IN (
                   SELECT record_rowid FROM (
                       SELECT tr.rowid AS record_rowid,
                              ROW_NUMBER() OVER (
                                  PARTITION BY tr.wallet_address
                                  ORDER BY COALESCE(ss.slot, -1) DESC,
                                           COALESCE(ss.first_seen_at, tr.updated_at) DESC,
                                           tr.updated_at DESC, tr.signature DESC
                              ) AS retention_rank
                       FROM transaction_records tr
                       LEFT JOIN seen_signatures ss
                         ON ss.wallet_address=tr.wallet_address
                        AND ss.signature=tr.signature
                       WHERE tr.wallet_address=?
                   ) ranked
                   WHERE retention_rank>?
               )""",
            (wallet_address, limit),
        )
        return cursor.rowcount

    @staticmethod
    def _validate_wallet(wallet_address):
        if not isinstance(wallet_address, str) or not wallet_address.strip():
            raise ValueError("A public wallet address is required.")

    @staticmethod
    def _validate_signature(signature):
        if not isinstance(signature, str) or not signature.strip():
            raise ValueError("A transaction signature is required.")

    def has_baseline(self, wallet_address):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM wallet_baselines
                WHERE wallet_address = ?
                LIMIT 1
                """,
                (wallet_address,),
            ).fetchone()

        return row is not None

    def mark_baseline(self, wallet_address):
        self._validate_wallet(wallet_address)

        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO wallet_baselines
                        (wallet_address)
                    VALUES (?)
                    """,
                    (wallet_address,),
                )
                created = cursor.rowcount == 1

        return created

    def has_seen(self, wallet_address, signature):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM seen_signatures
                WHERE wallet_address = ?
                  AND signature = ?
                LIMIT 1
                """,
                (wallet_address, signature),
            ).fetchone()

        return row is not None

    def get_status(self, wallet_address, signature):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT status
                FROM seen_signatures
                WHERE wallet_address = ?
                  AND signature = ?
                LIMIT 1
                """,
                (wallet_address, signature),
            ).fetchone()

        return row[0] if row else None

    def save(self, wallet_address, signature, slot=None, historical_baseline=False):
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)
        if slot is not None and (
            not isinstance(slot, int) or isinstance(slot, bool) or slot < 0
        ):
            raise ValueError("Slot must be a non-negative integer.")
        if not isinstance(historical_baseline, bool):
            raise ValueError("historical_baseline must be a boolean.")

        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_signatures
                        (wallet_address, signature, status, slot, historical_baseline)
                    VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (wallet_address, signature, slot, int(historical_baseline)),
                )
                saved = cursor.rowcount == 1
                if historical_baseline:
                    connection.execute(
                        """UPDATE seen_signatures SET historical_baseline=1
                           WHERE wallet_address=? AND signature=?""",
                        (wallet_address, signature),
                    )

        return saved

    def is_historical_baseline(self, wallet_address, signature):
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT historical_baseline FROM seen_signatures
                   WHERE wallet_address=? AND signature=?""",
                (wallet_address, signature),
            ).fetchone()
        return bool(row and row[0])

    def save_many(self, wallet_address, signatures):
        saved = 0

        for signature in signatures:
            if self.save(wallet_address, signature):
                saved += 1

        return saved

    def mark_processed(self, wallet_address, signature):
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)

        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE seen_signatures
                    SET status = 'processed',
                        last_checked_at = CURRENT_TIMESTAMP,
                        error_stage = NULL,
                        error_code = NULL,
                        error_message = NULL
                    WHERE wallet_address = ?
                      AND signature = ?
                    """,
                    (wallet_address, signature),
                )
                connection.execute(
                    """UPDATE transaction_records
                       SET processing_status = 'processed',
                           updated_at = CURRENT_TIMESTAMP
                       WHERE wallet_address = ? AND signature = ?""",
                    (wallet_address, signature),
                )
                position = connection.execute(
                    "SELECT slot FROM seen_signatures WHERE wallet_address = ? AND signature = ?",
                    (wallet_address, signature),
                ).fetchone()
                if position and position[0] is not None:
                    connection.execute(
                        """INSERT INTO wallet_positions (wallet_address, slot, signature)
                           VALUES (?, ?, ?)
                           ON CONFLICT(wallet_address) DO UPDATE SET
                             slot = CASE
                               WHEN wallet_positions.slot IS NULL OR excluded.slot >= wallet_positions.slot
                               THEN excluded.slot ELSE wallet_positions.slot END,
                             signature = CASE
                               WHEN wallet_positions.slot IS NULL OR excluded.slot >= wallet_positions.slot
                               THEN excluded.signature ELSE wallet_positions.signature END,
                             updated_at = CURRENT_TIMESTAMP""",
                        (wallet_address, position[0], signature),
                    )

        return cursor.rowcount == 1


    def record_failure(
        self, wallet_address, signature, stage, code, message,
        max_attempts=5,
    ):
        """Record a bounded retry state; repeated failures need manual review."""
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer.")
        allowed_stages = {"discovery", "rpc_fetch", "interpretation", "paper_simulation", "persistence"}
        if stage not in allowed_stages:
            raise ValueError("Invalid processing stage.")
        safe_code = code if isinstance(code, str) and code else "processing_error"
        safe_message = message if isinstance(message, str) else "Processing failed."
        safe_message = safe_message[:300]
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT attempts FROM seen_signatures WHERE wallet_address = ? AND signature = ?",
                    (wallet_address, signature),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO seen_signatures (wallet_address, signature, status, attempts) VALUES (?, ?, 'pending', 0)",
                        (wallet_address, signature),
                    )
                    attempts = 0
                else:
                    attempts = row[0]
                attempts += 1
                status = "needs_review" if attempts >= max_attempts else "pending"
                connection.execute(
                    """UPDATE seen_signatures
                       SET status = ?, attempts = ?, last_checked_at = CURRENT_TIMESTAMP,
                           error_stage = ?, error_code = ?, error_message = ?
                       WHERE wallet_address = ? AND signature = ?""",
                    (status, attempts, stage, safe_code, safe_message, wallet_address, signature),
                )
        return {"status": status, "attempts": attempts}

    def save_transaction_record(
        self, wallet_address, signature, raw_transaction=None,
        processing_status="observed",
    ):
        """Idempotently preserve public transaction data for investigation."""
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)
        if processing_status not in {"observed", "incomplete", "failed", "processed", "needs_review"}:
            raise ValueError("Invalid transaction processing status.")
        raw_json = None if raw_transaction is None else json.dumps(
            raw_transaction, sort_keys=True, separators=(",", ":"), default=str
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """INSERT INTO transaction_records
                       (wallet_address, signature, raw_transaction_json, processing_status)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(wallet_address, signature) DO UPDATE SET
                         raw_transaction_json = COALESCE(excluded.raw_transaction_json,
                                                         transaction_records.raw_transaction_json),
                         processing_status = excluded.processing_status,
                         updated_at = CURRENT_TIMESTAMP""",
                    (wallet_address, signature, raw_json, processing_status),
                )
                self._prune_transaction_records(
                    connection, wallet_address,
                    MAX_RAW_TRANSACTION_RECORDS_PER_WALLET,
                )
        return True

    def get_transaction_record(self, wallet_address, signature):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT raw_transaction_json, processing_status, updated_at
                   FROM transaction_records
                   WHERE wallet_address = ? AND signature = ?""",
                (wallet_address, signature),
            ).fetchone()
        if row is None:
            return None
        return {
            "raw_transaction": json.loads(row[0]) if row[0] is not None else None,
            "status": row[1],
            "updated_at": row[2],
        }

    def get_error_record(self, wallet_address, signature):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT status, attempts, last_checked_at, error_stage,
                          error_code, error_message
                   FROM seen_signatures WHERE wallet_address = ? AND signature = ?""",
                (wallet_address, signature),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(
            ("status", "attempts", "last_checked_at", "error_stage",
             "error_code", "error_message"),
            row,
        ))

    def get_pending_signatures(self, wallet_address, limit=1000):
        self._validate_wallet(wallet_address)

        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("Limit must be an integer.")

        if limit < 1:
            raise ValueError("Limit must be at least 1.")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT signature
                FROM seen_signatures
                WHERE wallet_address = ?
                  AND status = 'pending'
                ORDER BY first_seen_at ASC, rowid ASC
                LIMIT ?
                """,
                (wallet_address, limit),
            ).fetchall()

        return [row[0] for row in rows]


    def get_position(self, wallet_address):
        self._validate_wallet(wallet_address)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT slot, signature, updated_at FROM wallet_positions WHERE wallet_address = ?",
                (wallet_address,),
            ).fetchone()
        if row is None:
            return None
        return {"slot": row[0], "signature": row[1], "updated_at": row[2]}

    def get_seen_signatures(self, wallet_address, limit=1000):
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("Limit must be an integer.")

        if limit < 1:
            raise ValueError("Limit must be at least 1.")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT signature
                FROM seen_signatures
                WHERE wallet_address = ?
                ORDER BY first_seen_at DESC
                LIMIT ?
                """,
                (wallet_address, limit),
            ).fetchall()

        return [row[0] for row in rows]


if __name__ == "__main__":
    print("SignatureStore module loaded successfully.")
