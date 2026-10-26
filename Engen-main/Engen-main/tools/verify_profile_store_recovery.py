"""Run a disposable, end-to-end SQLite backup/restore recovery exercise.

This uses only temporary local files and synthetic records. It does not contact
Solana RPC, access a real user database, or enable trading.
"""
import tempfile
from pathlib import Path

from engine.profile_store import ProfileStore

PUBLIC_ADDRESS = "11111111111111111111111111111111"


def main():
    with tempfile.TemporaryDirectory(prefix="engen-recovery-check-") as temp_dir:
        root = Path(temp_dir)
        source_path = root / "source.db"
        backup_path = root / "backup.db"
        store = ProfileStore(source_path)

        profile_id = store.create_profile("Recovery verification")
        store.associate_source_wallet(profile_id, PUBLIC_ADDRESS)
        store.save_transaction(
            "recovery-check-signature",
            PUBLIC_ADDRESS,
            classification="UNKNOWN",
            status="confirmed",
            evidence={"test_fixture": True},
            profile_ids=[profile_id],
            observed_at="2026-10-26T00:00:00Z",
        )
        store.save_balance_snapshot(
            profile_id,
            PUBLIC_ADDRESS,
            PUBLIC_ADDRESS,
            "0",
            6,
            source="synthetic-recovery-test",
            observed_at="2026-10-26T00:00:01Z",
        )

        store.backup_to(backup_path)
        if not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise RuntimeError("Backup file was not created.")

        # Check the restored database independently before opening it via the app.
        import sqlite3

        with sqlite3.connect(backup_path) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Restored database integrity check failed: {integrity}")

        restored = ProfileStore(backup_path)
        profile = restored.get_profile(profile_id)
        transactions = restored.list_transactions(profile_id)
        balances = restored.current_balances(profile_id)
        holdings = restored.portfolio_holdings(profile_id)

        if not profile or profile["name"] != "Recovery verification":
            raise RuntimeError("Profile did not survive backup/restore.")
        if len(transactions) != 1 or transactions[0]["signature"] != "recovery-check-signature":
            raise RuntimeError("Transaction history did not survive backup/restore.")
        if len(balances) != 1 or balances[0]["raw_amount"] != "0":
            raise RuntimeError("Zero-balance snapshot did not survive backup/restore.")
        if len(holdings) != 1 or holdings[0]["amount"] != "0":
            raise RuntimeError("Restored portfolio aggregation did not match expected data.")

        print(
            "RECOVERY CHECK: PASS — SQLite integrity is ok; profile, transaction "
            "history, zero-balance snapshot, and portfolio aggregation survived "
            "backup/restore. Synthetic temporary data only."
        )


if __name__ == "__main__":
    main()
