"""Read-only monitoring of public Solana wallet signatures."""

from contextlib import nullcontext
import time
import math

from engine.config import (
    MAX_SOURCE_FUTURE_SKEW_SECONDS,
    load_max_source_age_seconds,
)
from engine.solana_public import (
    DEFAULT_RPC_URL,
    SolanaRPCError,
    _rpc_request,
)
from engine.signature_store import SignatureStore
from engine.solana_validation import is_valid_solana_address, is_valid_transaction_signature


MAX_DISCOVERY_PAGES = 5
MAX_FRESH_SIGNATURES_PER_SCAN = 10
MAX_HISTORY_SIGNATURES_PER_SCAN = 25
MAX_HISTORY_SIGNATURES_PER_WALLET = 1000
WALLET_SCAN_BUDGET_SECONDS = 10.0


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
    return_batches=False,
    now=None,
    deadline=None,
):
    """Persist signatures and return fresh copy candidates separately from bounded history."""
    if deadline is None:
        deadline = time.monotonic() + WALLET_SCAN_BUDGET_SECONDS
    if not is_valid_solana_address(wallet_address):
        raise ValueError("Invalid Solana public wallet address.")
    if store is None:
        store = SignatureStore()

    cooldown_reader = getattr(store, "get_global_rpc_cooldown_remaining", None)
    if callable(cooldown_reader):
        try:
            if cooldown_reader() > 0:
                return {"fresh": [], "history": []} if return_batches else []
        except (TypeError, ValueError, OSError, RuntimeError):
            pass

    options = {
        "wallet_address": wallet_address,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        options["latency_recorder"] = latency_recorder
    discovery_error = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            signatures = []
            discovery_error = SolanaRPCError("Wallet scan budget exhausted before discovery.")
        else:
            options["timeout"] = min(timeout, remaining, 5.0)
            signatures = get_wallet_signatures(**options)
    except SolanaRPCError as exc:
        # Persist a provider-wide cooldown even when this is the first baseline
        # scan, so another wallet cannot immediately hit the same throttled RPC.
        diagnostics = getattr(exc, "diagnostics", {})
        cooldown = diagnostics.get("cooldown_seconds") if isinstance(diagnostics, dict) else None
        setter = getattr(store, "set_global_rpc_cooldown", None)
        if callable(setter) and isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool):
            try:
                setter(cooldown)
            except (TypeError, ValueError, OSError, RuntimeError):
                pass
        # The first scan still needs a successful read to establish its baseline.
        if not store.has_baseline(wallet_address):
            raise
        # A discovery outage must not block work that was already persisted.
        discovery_error = exc
        signatures = []

    # The first scan is historical baseline only; never create trade signals.
    if not store.has_baseline(wallet_address):
        if discovery_error is not None:
            raise discovery_error
        for item in signatures:
            if not isinstance(item, dict):
                continue
            signature = item.get("signature")
            if not is_valid_transaction_signature(signature):
                continue
            store.save(
                wallet_address, signature, slot=item.get("slot"),
                block_time=item.get("blockTime"),
            )
            store.mark_processed(wallet_address, signature)
        store.mark_baseline(wallet_address)
        return []

    # Poll newest-first and page backwards until the last successfully
    # processed signature is found. The page cap bounds provider load while
    # allowing recovery after a short outage or a busy source wallet.
    position = store.get_position(wallet_address)
    anchor_signature = position.get("signature") if position else None
    records_by_signature = {}
    if discovery_error is None:
        pages = 0
        page = signatures
        while True:
            if deadline - time.monotonic() <= 0:
                break
            pages += 1
            anchor_found = False
            for item in page:
                if not isinstance(item, dict):
                    continue
                signature = item.get("signature")
                if not is_valid_transaction_signature(signature):
                    continue
                records_by_signature[signature] = item
                store.save(
                    wallet_address, signature, slot=item.get("slot"),
                    block_time=item.get("blockTime"),
                )
                if anchor_signature and signature == anchor_signature:
                    anchor_found = True
                    break
            if anchor_found or len(page) < limit or pages >= MAX_DISCOVERY_PAGES or not page:
                break
            next_before = page[-1].get("signature") if isinstance(page[-1], dict) else None
            if not is_valid_transaction_signature(next_before):
                break
            page_options = {
                "wallet_address": wallet_address,
                "limit": limit,
                "rpc_url": rpc_url,
                "timeout": min(timeout, max(0.1, deadline - time.monotonic()), 5.0),
                "before": next_before,
            }
            if latency_recorder is not None:
                page_options["latency_recorder"] = latency_recorder
            try:
                page = get_wallet_signatures(**page_options)
            except SolanaRPCError as exc:
                diagnostics = getattr(exc, "diagnostics", {})
                cooldown = diagnostics.get("cooldown_seconds") if isinstance(diagnostics, dict) else None
                setter = getattr(store, "set_global_rpc_cooldown", None)
                if callable(setter) and isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool):
                    try:
                        setter(cooldown)
                    except (TypeError, ValueError, OSError, RuntimeError):
                        pass
                # Keep signatures already collected and continue processing the
                # durable queue; the next scan can resume discovery from its anchor.
                discovery_error = exc
                break

    # If discovery hit a provider cooldown, do not spend the remaining wallet
    # budget retrying each pending signature against the same throttled endpoint.
    cooldown_reader = getattr(store, "get_global_rpc_cooldown_remaining", None)
    if callable(cooldown_reader):
        try:
            if cooldown_reader() > 0:
                return {"fresh": [], "history": []} if return_batches else []
        except (TypeError, ValueError, OSError, RuntimeError):
            pass

    # Recover only explicitly transient needs_review entries, once per signature,
    # after the RPC cooldown, and only inside the wallet's newest 1,000-signature
    # history window.
    requeue = getattr(store, "requeue_recoverable_needs_review", None)
    if callable(requeue):
        try:
            requeue(
                wallet_address,
                limit=10,
                cooldown_seconds=30,
                history_cap=MAX_HISTORY_SIGNATURES_PER_WALLET,
            )
        except (TypeError, ValueError, OSError, RuntimeError):
            pass

    current_time = time.time() if now is None else now
    max_source_age = load_max_source_age_seconds()
    fresh_reader = getattr(store, "get_fresh_pending_signatures", None)
    fresh = []
    if callable(fresh_reader):
        try:
            fresh = fresh_reader(
                wallet_address,
                limit=MAX_FRESH_SIGNATURES_PER_SCAN,
                max_age_seconds=max_source_age,
                future_skew_seconds=MAX_SOURCE_FUTURE_SKEW_SECONDS,
                now=current_time,
            )
        except (TypeError, ValueError, OSError, RuntimeError):
            fresh = []
    else:
        # Compatibility fallback for legacy test doubles; production stores the
        # block_time column and retrieves pending fresh observations durably.
        for item in records_by_signature.values():
            block_time = item.get("blockTime")
            if isinstance(block_time, int) and not isinstance(block_time, bool):
                age = current_time - block_time
                if -5 <= age <= max_source_age:
                    fresh.append(dict(item))
        fresh = fresh[:MAX_FRESH_SIGNATURES_PER_SCAN]

    fresh = [
        {
            **records_by_signature.get(item["signature"], item),
            "copy_eligible": True,
            "history_backfill": False,
        }
        for item in fresh
        if isinstance(item, dict) and isinstance(item.get("signature"), str)
    ]
    fresh_signatures = [item["signature"] for item in fresh]
    history_reader = getattr(store, "get_history_pending_signatures", None)
    if callable(history_reader):
        try:
            history_signatures = history_reader(
                wallet_address,
                limit=MAX_HISTORY_SIGNATURES_PER_SCAN,
                history_cap=MAX_HISTORY_SIGNATURES_PER_WALLET,
                exclude_signatures=fresh_signatures,
                before_block_time=math.ceil(current_time - max_source_age),
            )
        except (TypeError, ValueError, OSError, RuntimeError):
            history_signatures = []
    else:
        # Compatibility fallback for older test doubles; production SignatureStore
        # always uses the newest-1,000 bounded history selector above.
        history_signatures = store.get_pending_signatures(
            wallet_address, limit=MAX_HISTORY_SIGNATURES_PER_SCAN
        )
        history_signatures = [
            signature for signature in history_signatures
            if signature not in set(fresh_signatures)
        ][:MAX_HISTORY_SIGNATURES_PER_SCAN]

    history = [
        {
            **records_by_signature.get(signature, {"signature": signature}),
            "copy_eligible": False,
            "history_backfill": True,
        }
        for signature in history_signatures
    ]
    if discovery_error is not None and not fresh and not history:
        raise discovery_error

    if discovery_error is not None:
        warning = (
            "New signature discovery failed; saved bounded history is still being processed."
        )
        for item in fresh + history:
            item["discovery_warning"] = warning

    if return_batches:
        return {"fresh": fresh, "history": history}
    # Preserve the original flat-list API for callers that have not opted into
    # the explicit Fresh/History result structure.
    legacy_results = []
    for item in fresh + history:
        legacy_results.append({
            key: value for key, value in item.items()
            if key not in {"copy_eligible", "history_backfill"}
        })
    return legacy_results


if __name__ == "__main__":
    print("Read-only wallet monitor loaded successfully.")
