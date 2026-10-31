"""Read-only monitoring of public Solana wallet signatures."""

from contextlib import nullcontext

from engine.solana_public import (
    DEFAULT_RPC_URL,
    SolanaRPCError,
    _rpc_request,
)
from engine.signature_store import SignatureStore
from engine.solana_validation import is_valid_solana_address, is_valid_transaction_signature


def get_wallet_signatures(
    wallet_address,
    limit=10,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    before=None,
    latency_recorder=None,
):
    """Fetch a bounded list of signatures for a public wallet."""
    if not is_valid_solana_address(wallet_address):
        raise ValueError("Invalid Solana public wallet address.")

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("Limit must be an integer.")
    if not 1 <= limit <= 1000:
        raise ValueError("Limit must be between 1 and 1000.")

    if before is not None and (
        not isinstance(before, str) or not before.strip()
    ):
        raise ValueError("Before must be a non-empty transaction signature.")

    options = {"limit": limit}
    if before is not None:
        options["before"] = before

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [wallet_address, options],
    }
    timer = (
        latency_recorder.measure("wallet.signature_fetch")
        if latency_recorder is not None
        else nullcontext()
    )
    try:
        with timer:
            result = _rpc_request(payload, rpc_url=rpc_url, timeout=timeout)
    except SolanaRPCError:
        raise

    if not isinstance(result, list):
        raise SolanaRPCError("Solana RPC returned an invalid signature list.")

    # Preserve provider metadata (slot, err, confirmationStatus, blockTime),
    # but drop malformed rows so they cannot become transaction records.
    valid_rows = []
    for item in result:
        if not isinstance(item, dict):
            continue
        signature = item.get("signature")
        if not is_valid_transaction_signature(signature):
            continue
        slot = item.get("slot")
        if slot is not None and (
            not isinstance(slot, int) or isinstance(slot, bool) or slot < 0
        ):
            continue
        valid_rows.append(item)
    return valid_rows


def filter_new_signatures(signatures, seen_signatures):
    """Return valid signatures not present in the supplied seen list."""
    seen = set(seen_signatures)
    new_signatures = []
    for item in signatures:
        if not isinstance(item, dict):
            continue
        signature = item.get("signature")
        if (
            isinstance(signature, str)
            and signature.strip()
            and signature not in seen
        ):
            new_signatures.append(item)
            seen.add(signature)
    return new_signatures


def get_new_wallet_signatures(
    wallet_address,
    seen_signatures,
    limit=10,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    latency_recorder=None,
):
    """Fetch recent signatures and filter previously seen entries."""
    options = {
        "wallet_address": wallet_address,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        options["latency_recorder"] = latency_recorder
    signatures = get_wallet_signatures(**options)
    return filter_new_signatures(signatures, seen_signatures)


def get_and_store_new_wallet_signatures(
    wallet_address,
    store=None,
    limit=10,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    latency_recorder=None,
):
    """Persist signatures, establish baseline, and return pending entries."""
    if not is_valid_solana_address(wallet_address):
        raise ValueError("Invalid Solana public wallet address.")
    if store is None:
        store = SignatureStore()

    options = {
        "wallet_address": wallet_address,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        options["latency_recorder"] = latency_recorder
    signatures = get_wallet_signatures(**options)

    # The first scan seeds a historical baseline. Keep entries pending so
    # they can be analyzed and shown in History, but persist a flag that
    # prevents them from creating paper-copy orders.
    if not store.has_baseline(wallet_address):
        baseline_records = {}
        for item in signatures:
            if not isinstance(item, dict):
                continue
            signature = item.get("signature")
            if not is_valid_transaction_signature(signature):
                continue
            store.save(
                wallet_address, signature, slot=item.get("slot"),
                historical_baseline=True,
            )
            baseline_records[signature] = item
        store.mark_baseline(wallet_address)
        pending = store.get_pending_signatures(wallet_address, limit=limit)
        return [
            {
                **baseline_records.get(signature, {"signature": signature}),
                **(
                    {"historical_baseline": True}
                    if store.is_historical_baseline(wallet_address, signature)
                    else {}
                ),
            }
            for signature in pending
        ]

    # Poll newest-first and page backwards until the last successfully
    # processed signature is found. The page cap bounds provider load while
    # allowing recovery after a short outage or a busy source wallet.
    position = store.get_position(wallet_address)
    anchor_signature = position.get("signature") if position else None
    records_by_signature = {}
    page = signatures
    seen_cursors = set()
    while True:
        anchor_found = False
        for item in page:
            if not isinstance(item, dict):
                continue
            signature = item.get("signature")
            if not is_valid_transaction_signature(signature):
                continue
            records_by_signature[signature] = item
            store.save(wallet_address, signature, slot=item.get("slot"))
            if anchor_signature and signature == anchor_signature:
                anchor_found = True
                break
        if anchor_found or len(page) < limit or not page:
            break
        next_before = page[-1].get("signature") if isinstance(page[-1], dict) else None
        if (
            not is_valid_transaction_signature(next_before)
            or next_before in seen_cursors
        ):
            # Protect against providers that repeat a page/cursor indefinitely.
            break
        seen_cursors.add(next_before)
        page_options = {
            "wallet_address": wallet_address,
            "limit": limit,
            "rpc_url": rpc_url,
            "timeout": timeout,
            "before": next_before,
        }
        if latency_recorder is not None:
            page_options["latency_recorder"] = latency_recorder
        page = get_wallet_signatures(**page_options)

    pending_signatures = store.get_pending_signatures(
        wallet_address, limit=limit
    )
    return [
        {
            **records_by_signature.get(signature, {"signature": signature}),
            **(
                {"historical_baseline": True}
                if store.is_historical_baseline(wallet_address, signature)
                else {}
            ),
        }
        for signature in pending_signatures
    ]


if __name__ == "__main__":
    print("Read-only wallet monitor loaded successfully.")
