
"""Bridge between the dashboard and the read-only Solana engine."""

from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet
from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders


def create_scan_function(limit=10, timeout=10, side_getter=None, settings_getter=None, simulator_getter=None):
    """Create a scanner with profile-specific settings and isolated paper ledger."""
    store = SignatureStore()

    def scan_wallet(address):
        """Analyze new wallet signatures and queue eligible paper-copy orders."""
        results = monitor_wallet(
            wallet_address=address,
            store=store,
            limit=limit,
            timeout=timeout,
            defer_processed=True,
        )
        try:
            allowed_sides = side_getter() if side_getter is not None else "BOTH"
            copy_settings = settings_getter() if settings_getter is not None else {}
            simulator = simulator_getter() if simulator_getter is not None else None
            queued_orders = queue_paper_orders(
                address, results, simulator=simulator,
                allowed_sides=allowed_sides, copy_settings=copy_settings
            )
            execution_outcomes = execute_pending_paper_orders(address, simulator=simulator)
            outcomes_by_signal = {
                item["signal_id"]: item for item in execution_outcomes
                if item.get("signal_id")
            }
            # Queue persistence succeeded. Mark complete, non-retryable scans
            # processed so unsupported transactions do not loop forever.
            for item in results:
                signature = item.get("signature")
                if (
                    isinstance(signature, str)
                    and item.get("retryable") is not True
                ):
                    store.mark_processed(address, signature)
            if queued_orders:
                # Keep the monitor response auditable without exposing raw transactions.
                queued_by_signature = {
                    item["signature"]: item for item in queued_orders
                }
                results = [
                    {
                        **item,
                        **(
                            {
                                "paper_copy_order": {
                                    **queued_by_signature[item.get("signature")],
                                    **(
                                        {"execution_status": outcomes_by_signal[
                                            "wallet-copy:" + address + ":" + item.get("signature")
                                        ]["status"]}
                                        if (
                                            "wallet-copy:" + address + ":" + item.get("signature")
                                            in outcomes_by_signal
                                        )
                                        else {}
                                    ),
                                }
                            }
                            if item.get("signature") in queued_by_signature
                            else {}
                        ),
                    }
                    for item in results
                ]
        except Exception:
            # If queue persistence fails, leave potential swap candidates pending
            # so the next scan can retry; mark only clearly non-candidate results.
            for item in results:
                signature = item.get("signature")
                signal = item.get("trade_signal")
                is_candidate = (
                    isinstance(signal, dict)
                    and signal.get("classification") in ("BUY_CANDIDATE", "SELL_CANDIDATE")
                )
                if (
                    isinstance(signature, str)
                    and item.get("retryable") is not True
                    and not is_candidate
                ):
                    store.mark_processed(address, signature)
            results = [
                {**item, "paper_copy_error": "Paper-copy order queue failed."}
                for item in results
            ]
        return results

    return scan_wallet
