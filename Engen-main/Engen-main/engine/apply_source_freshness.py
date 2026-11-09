"""Apply source-freshness gate onto paper_copy_runner.queue_paper_orders.

Imported by server_with_pending so the gate is active without rewriting the
large paper_copy_runner module in-place.
"""

from engine import paper_copy_runner as pcr
from engine.source_freshness import source_block_time_ok

_orig_queue = pcr.queue_paper_orders


def queue_paper_orders(wallet_address, results, *args, **kwargs):
    if not isinstance(results, list):
        return _orig_queue(wallet_address, results, *args, **kwargs)
    filtered = []
    for result in results:
        if not isinstance(result, dict):
            continue
        observation = result.get("signature_observation")
        if not source_block_time_ok(
            observation if isinstance(observation, dict) else {}
        ):
            continue
        filtered.append(result)
    return _orig_queue(wallet_address, filtered, *args, **kwargs)


def apply():
    pcr.queue_paper_orders = queue_paper_orders


apply()
