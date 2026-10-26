
"""Bridge between the dashboard and the read-only Solana engine."""

from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet


def create_scan_function(limit=10, timeout=10):
    """Create a scanner that reuses persistent signature history."""
    store = SignatureStore()

    def scan_wallet(address):
        """Read public wallet activity and analyze newly found signatures."""
        return monitor_wallet(
            wallet_address=address,
            store=store,
            limit=limit,
            timeout=timeout,
        )

    return scan_wallet
