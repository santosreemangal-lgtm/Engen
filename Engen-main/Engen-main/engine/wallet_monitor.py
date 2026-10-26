"""Read-only monitoring of public Solana wallet signatures."""

import json
import urllib.error
import urllib.request
from contextlib import nullcontext

from engine.solana_public import (
    DEFAULT_RPC_URL,
    SolanaRPCError,
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
    """Fetch signatures for a public wallet.

    If before is provided, fetch signatures older than that signature.
    This function only reads public RPC data.
    """
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A public wallet address is required.")

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("Limit must be an integer.")

    if not 1 <= limit <= 1000:
        raise ValueError("Limit must be between 1 and 1000.")

    if before is not None:
        if not isinstance(before, str) or not before.strip():
            raise ValueError(
                "Before must be a non-empty transaction signature."
            )

    options = {"limit": limit}

    if before is not None:
        options["before"] = before

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            wallet_address,
            options,
        ],
    }).encode("utf-8")

    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    timer = (
        latency_recorder.measure("wallet.signature_fetch")
        if latency_recorder is not None
        else nullcontext()
    )
    try:
        with timer:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:
                data = json.loads(
                    response.read().decode("utf-8")
                )
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        # Do not expose the configured RPC URL or credentials in error text.
        raise SolanaRPCError(
            "Failed to fetch wallet signatures from the configured RPC endpoint."
        ) from None

    if not isinstance(data, dict):
        raise SolanaRPCError(
            "Solana RPC returned an invalid response."
        )

    if "error" in data:
        raise SolanaRPCError(
            "Solana RPC returned an error."
        )

    result = data.get("result")

    if not isinstance(result, list):
        raise SolanaRPCError(
            "Solana RPC returned an invalid signature list."
        )

    return result


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
    signature_options = {
        "wallet_address": wallet_address,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        signature_options["latency_recorder"] = latency_recorder
    signatures = get_wallet_signatures(**signature_options)

    return filter_new_signatures(
        signatures,
        seen_signatures,
    )


def get_and_store_new_wallet_signatures(
    wallet_address,
    store=None,
    limit=10,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    latency_recorder=None,
):
    """Persist signatures, establish the initial baseline, and retry pending entries."""
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A public wallet address is required.")

    if store is None:
        store = SignatureStore()

    signature_options = {
        "wallet_address": wallet_address,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        signature_options["latency_recorder"] = latency_recorder
    signatures = get_wallet_signatures(**signature_options)

    # The first scan creates a historical baseline.
    # Historical signatures must not become new paper-trading signals.
    if not store.has_baseline(wallet_address):
        valid_signatures = []

        for item in signatures:
            if not isinstance(item, dict):
                continue

            signature = item.get("signature")

            if (
                isinstance(signature, str)
                and signature.strip()
            ):
                valid_signatures.append(signature)

        store.save_many(wallet_address, valid_signatures)

        for signature in valid_signatures:
            store.mark_processed(wallet_address, signature)

        store.mark_baseline(wallet_address)
        return []

    # Save newly observed signatures as pending.
    # Existing records retain their current status.
    records_by_signature = {}

    for item in signatures:
        if not isinstance(item, dict):
            continue

        signature = item.get("signature")

        if (
            not isinstance(signature, str)
            or not signature.strip()
        ):
            continue

        records_by_signature[signature] = item
        store.save(wallet_address, signature)

    # Include pending signatures from earlier scans so they can be retried.
    pending_signatures = store.get_pending_signatures(
        wallet_address,
        limit=limit,
    )

    pending_records = []

    for signature in pending_signatures:
        record = records_by_signature.get(
            signature,
            {"signature": signature},
        )
        pending_records.append(record)

    return pending_records


if __name__ == "__main__":
    print("Read-only wallet monitor loaded successfully.")