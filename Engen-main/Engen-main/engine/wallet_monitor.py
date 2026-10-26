"""Read-only monitoring of public Solana wallet signatures."""

from contextlib import nullcontext

from engine.solana_public import (
    DEFAULT_RPC_URL,
    SolanaRPCError,
    _rpc_request,
)
from engine.signature_store import SignatureStore


def get_wallet_signatures(
    wallet_address,
    limit=10,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    before=None,
    latency_recorder=None,
):
    """Fetch a bounded list of signatures for a public wallet."""
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A public wallet address is required.")

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
        if not isinstance(signature, str) or not signature.strip():
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
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A public wallet address is required.")
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

    # The first scan is historical baseline only; never create trade signals.
    if not store.has_baseline(wallet_address):
        valid_signatures = [
            item["signature"] for item in signatures
            if isinstance(item, dict)
            and isinstance(item.get("signature"), str)
            and item["signature"].strip()
        ]
        store.save_many(wallet_address, valid_signatures)
        for signature in valid_signatures:
            store.mark_processed(wallet_address, signature)
        store.mark_baseline(wallet_address)
        return []

    records_by_signature = {}
    for item in signatures:
        if not isinstance(item, dict):
            continue
        signature = item.get("signature")
        if not isinstance(signature, str) or not signature.strip():
            continue
        records_by_signature[signature] = item
        store.save(wallet_address, signature)

    pending_signatures = store.get_pending_signatures(
        wallet_address, limit=limit
    )
    return [
        records_by_signature.get(signature, {"signature": signature})
        for signature in pending_signatures
    ]


if __name__ == "__main__":
    print("Read-only wallet monitor loaded successfully.")
