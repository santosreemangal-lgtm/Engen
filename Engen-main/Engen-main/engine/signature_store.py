
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

                migrations = {
                    "status": "TEXT NOT NULL DEFAULT 'processed'",
                    "attempts": "INTEGER NOT NULL DEFAULT 0",
                    "slot": "INTEGER",
                    "last_checked_at": "TEXT",
                    "error_stage": "TEXT",
                    "error_code": "TEXT",
                    "error_message": "TEXT",
                    "requeue_count": "INTEGER NOT NULL DEFAULT 0",
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
                connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_seen_signatures_wallet_status_first_seen
                    ON seen_signatures(wallet_address, status, first_seen_at)
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

    def get_statuses(self, wallet_address, signatures):
        """Read a bounded batch of signature statuses using one database connection."""
        self._validate_wallet(wallet_address)
        if not isinstance(signatures, (list, tuple)) or len(signatures) > 1000:
            raise ValueError("Signature status batch must contain at most 1000 items.")
        cleaned = []
        for signature in signatures:
            self._validate_signature(signature)
            if signature not in cleaned:
                cleaned.append(signature)
        if not cleaned:
            return {}
        statuses = {}
        # Keep each query below SQLite builds configured with the traditional
        # 999-variable limit, while still reusing one database connection.
        with closing(self._connect()) as connection:
            for start in range(0, len(cleaned), 400):
                batch = cleaned[start:start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""SELECT signature, status FROM seen_signatures
                        WHERE wallet_address = ? AND signature IN ({placeholders})""",
                    [wallet_address, *batch],
                ).fetchall()
                statuses.update({
                    signature: status for signature, status in rows
                })
        return statuses


    def save(self, wallet_address, signature, slot=None):
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)
        if slot is not None and (
            not isinstance(slot, int) or isinstance(slot, bool) or slot < 0
        ):
            raise ValueError("Slot must be a non-negative integer.")

        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_signatures
                        (wallet_address, signature, status, slot)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (wallet_address, signature, slot),
                )
                saved = cursor.rowcount == 1

        return saved

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
                      AND status != 'needs_review'
                    """,
                    (wallet_address, signature),
                )
                if cursor.rowcount == 1:
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
                else:
                    position = None
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
                if status == "needs_review":
                    connection.execute(
                        """UPDATE transaction_records
                           SET processing_status = 'needs_review',
                               updated_at = CURRENT_TIMESTAMP
                           WHERE wallet_address = ? AND signature = ?""",
                        (wallet_address, signature),
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
                ORDER BY first_seen_at DESC, signature DESC
                LIMIT ?
                """,
                (wallet_address, limit),
            ).fetchall()

        return [row[0] for row in rows]

    def get_history_pending_signatures(
        self, wallet_address, limit=25, history_cap=1000, exclude_signatures=()
    ):
        """Select newest pending history only from the wallet's newest bounded window."""
        self._validate_wallet(wallet_address)
        if (
            not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25
            or not isinstance(history_cap, int) or isinstance(history_cap, bool)
            or not 1 <= history_cap <= 1000
            or not isinstance(exclude_signatures, (list, tuple))
            or len(exclude_signatures) > 50
        ):
            raise ValueError("Invalid bounded history selection.")
        excluded = []
        for signature in exclude_signatures:
            self._validate_signature(signature)
            if signature not in excluded:
                excluded.append(signature)
        exclusion_sql = ""
        params = [wallet_address, history_cap]
        if excluded:
            exclusion_sql = " AND signature NOT IN (" + ",".join("?" for _ in excluded) + ")"
            params.extend(excluded)
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                WITH newest_window AS (
                    SELECT signature, status, first_seen_at
                    FROM seen_signatures
                    WHERE wallet_address = ?
                    ORDER BY first_seen_at DESC, signature DESC
                    LIMIT ?
                )
                SELECT signature
                FROM newest_window
                WHERE status = 'pending'""" + exclusion_sql + """
                ORDER BY first_seen_at DESC, signature DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [row[0] for row in rows]

    def requeue_recoverable_needs_review(
        self, wallet_address, limit=10, cooldown_seconds=30, history_cap=1000
    ):
        """Requeue only known transient failures once, after cooldown, within recent history."""
        self._validate_wallet(wallet_address)
        if (
            not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10
            or not isinstance(cooldown_seconds, int) or isinstance(cooldown_seconds, bool)
            or cooldown_seconds < 30
            or not isinstance(history_cap, int) or isinstance(history_cap, bool)
            or not 1 <= history_cap <= 1000
        ):
            raise ValueError("Invalid bounded needs-review requeue policy.")
        cutoff = "-%d seconds" % cooldown_seconds
        recoverable_codes = ("http_429", "http_error", "timeout", "network_error")
        with closing(self._connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    WITH newest_window AS (
                        SELECT wallet_address, signature, status, error_code,
                               last_checked_at, requeue_count, first_seen_at
                        FROM seen_signatures
                        WHERE wallet_address = ?
                        ORDER BY first_seen_at DESC, signature DESC
                        LIMIT ?
                    )
                    SELECT signature
                    FROM newest_window
                    WHERE status = 'needs_review'
                      AND requeue_count < 1
                      AND error_code IN (?,?,?,?)
                      AND last_checked_at <= datetime('now', ?)
                    ORDER BY last_checked_at ASC, first_seen_at DESC, signature DESC
                    LIMIT ?
                    """,
                    (wallet_address, history_cap, *recoverable_codes, cutoff, limit),
                ).fetchall()
                signatures = [row[0] for row in rows]
                for signature in signatures:
                    connection.execute(
                        """
                        UPDATE seen_signatures
                        SET status='pending', attempts=0,
                            last_checked_at=CURRENT_TIMESTAMP,
                            requeue_count=requeue_count+1
                        WHERE wallet_address=? AND signature=?
                          AND status='needs_review' AND requeue_count < 1
                        """,
                        (wallet_address, signature),
                    )
                    connection.execute(
                        """
                        UPDATE transaction_records
                        SET processing_status='incomplete',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE wallet_address=? AND signature=?
                          AND processing_status='needs_review'
                        """,
                        (wallet_address, signature),
                    )
        return signatures

    def get_backlog_stats(self, wallet_address):
        """Return per-wallet queue counts and the age of its oldest pending item."""
        self._validate_wallet(wallet_address)
        with closing(self._connect()) as connection:
            status_rows = connection.execute(
                """SELECT status, COUNT(*)
                   FROM seen_signatures
                   WHERE wallet_address = ?
                   GROUP BY status""",
                (wallet_address,),
            ).fetchall()
            oldest = connection.execute(
                """SELECT first_seen_at,
                          MAX(0, CAST(
                              (julianday('now') - julianday(first_seen_at)) * 86400
                              AS INTEGER
                          ))
                   FROM seen_signatures
                   WHERE wallet_address = ? AND status = 'pending'
                   ORDER BY first_seen_at ASC
                   LIMIT 1""",
                (wallet_address,),
            ).fetchone()
            retry_pressure = connection.execute(
                """SELECT COUNT(*), COALESCE(MAX(attempts), 0)
                   FROM seen_signatures
                   WHERE wallet_address = ? AND status = 'pending' AND attempts >= 3""",
                (wallet_address,),
            ).fetchone()

        counts = {status: count for status, count in status_rows}
        return {
            "total_count": sum(counts.values()),
            "pending_count": counts.get("pending", 0),
            "processed_count": counts.get("processed", 0),
            "needs_review_count": counts.get("needs_review", 0),
            "oldest_pending_at": oldest[0] if oldest else None,
            "oldest_pending_age_seconds": oldest[1] if oldest else None,
            "pending_retry_pressure_count": retry_pressure[0],
            "pending_max_attempts": retry_pressure[1],
        }


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
