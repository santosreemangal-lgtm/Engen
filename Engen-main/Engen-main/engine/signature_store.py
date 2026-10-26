
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
                    SET status = 'processed'
                    WHERE wallet_address = ?
                      AND signature = ?
                    """,
                    (wallet_address, signature),
                )

        return cursor.rowcount == 1

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
