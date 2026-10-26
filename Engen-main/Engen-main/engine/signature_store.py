
import json
import sqlite3
from contextlib import closing
from pathlib import Path


DEFAULT_DB_PATH = Path("data/signatures.db")


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
                    "last_checked_at": "TEXT",
                    "error_stage": "TEXT",
                    "error_code": "TEXT",
                    "error_message": "TEXT",
                }
                for column, definition in migrations.items():
                    if column not in columns:
                        connection.execute(
                            "ALTER TABLE seen_signatures ADD COLUMN "
                            + column + " " + definition
                        )

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

    def save(self, wallet_address, signature):
        self._validate_wallet(wallet_address)
        self._validate_signature(signature)

        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_signatures
                        (wallet_address, signature, status)
                    VALUES (?, ?, 'pending')
                    """,
                    (wallet_address, signature),
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
                ORDER BY first_seen_at ASC
                LIMIT ?
                """,
                (wallet_address, limit),
            ).fetchall()

        return [row[0] for row in rows]

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
