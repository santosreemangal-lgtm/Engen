
"""Bridge between the dashboard and the read-only Solana engine."""

from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet
from engine.paper_copy_runner import queue_paper_orders


def create_scan_function(limit=10, timeout=10):
    """Create a scanner that reuses persistent signature history."""
    store = SignatureStore()

    def scan_wallet(address):
        """Analyze new wallet signatures and queue eligible paper-copy orders."""
        results = monitor_wallet(
            wallet_address=address,
            store=store,
            limit=limit,
            timeout=timeout,
        )
        try:
            queued_orders = queue_paper_orders(address, results)
            if queued_orders:
                # Keep the monitor response auditable without exposing raw transactions.
                queued_by_signature = {
                    item["signature"]: item for item in queued_orders
                }
                results = [
                    {
                        **item,
                        **(
                            {"paper_copy_order": queued_by_signature[item.get("signature")]}
                            if item.get("signature") in queued_by_signature
                            else {}
                        ),
                    }
                    for item in results
                ]
        except Exception:
            # Monitoring remains available, but paper-order failures must be
            # visible in the scan result instead of silently claiming success.
            results = [
                {**item, "paper_copy_error": "Paper-copy order queue failed."}
                for item in results
            ]
        return results

    return scan_wallet
