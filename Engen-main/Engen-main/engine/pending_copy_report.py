"""Pending automatic paper-copy status for dashboard reports.

Safe helper: no signing, no broadcast, paper-only.
"""

from engine.paper_copy_runner import get_pending_copy_status, PAPER_SIMULATOR_LOCK


def attach_pending_copy(report, simulator=None):
    """Attach pending_copy summary onto an existing paper-simulation report dict."""
    if not isinstance(report, dict):
        return report
    try:
        with PAPER_SIMULATOR_LOCK:
            pending = get_pending_copy_status(simulator=simulator)
        report["pending_copy"] = {
            "pending_count": pending.get("pending_count", 0),
            "pending_orders": pending.get("pending_orders", [])[:20],
            "real_trading": False,
            "transaction_broadcast": False,
        }
    except Exception:
        report["pending_copy"] = {
            "pending_count": 0,
            "pending_orders": [],
            "error": "pending_status_unavailable",
            "real_trading": False,
            "transaction_broadcast": False,
        }
    return report
