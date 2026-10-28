"""Bridge between the dashboard and the read-only Solana engine."""

from pathlib import Path

from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIGNATURE_DB_PATH = PROJECT_ROOT / "data" / "signatures.db"


def create_scan_function(limit=10, timeout=10):
    """Create a scanner that leaves new signatures pending until paper handling."""
    store = SignatureStore(SIGNATURE_DB_PATH)

    def scan_wallet(address):
        """Read public wallet activity and analyze newly found signatures."""
        return monitor_wallet(
            wallet_address=address,
            store=store,
            limit=limit,
            timeout=timeout,
            defer_processed=True,
        )

    def mark_processed(address, signature):
        """Acknowledge a signature only after its paper signal was handled."""
        store.mark_processed(address, signature)

    scan_wallet.mark_processed = mark_processed
    return scan_wallet
