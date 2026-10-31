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
    max_pages=100,
):
    """Persist signatures and backfill to a safe checkpoint using bounded resumable pagination."""
    if not is_valid_solana_address(wallet_address):
        raise ValueError("Invalid Solana public wallet address.")
    if store is None:
        store = SignatureStore()
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be an integer from 1 to 1000.")

    options = {
        "wallet_address": wallet_address,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        options["latency_recorder"] = latency_recorder
    first_page = get_wallet_signatures(**options)
    if not isinstance(first_page, list):
        raise TypeError("Wallet signature RPC must return a list.")

    baseline_mode = not store.has_baseline(wallet_address)
    mode = "baseline" if baseline_mode else "catchup"
    progress = store.get_scan_progress(wallet_address)
    if progress and progress.get("mode") != mode:
        store.clear_scan_progress(wallet_address)
        progress = None

    position = store.get_position(wallet_address)
    anchor_signature = (
        progress.get("anchor_signature")
        if progress and mode == "catchup"
        else (position.get("signature") if position else None)
    )
    records_by_signature = {}
    newest_record = None
    anchor_found = False

    def save_page(page):
        nonlocal newest_record, anchor_found
        for item in page:
            if not isinstance(item, dict):
                continue
            signature = item.get("signature")
            if not is_valid_transaction_signature(signature):
                continue
            if newest_record is None:
                newest_record = item
            store.save(
                wallet_address,
                signature,
                slot=item.get("slot"),
                historical_baseline=baseline_mode,
            )
            records_by_signature[signature] = item
            if anchor_signature and signature == anchor_signature:
                anchor_found = True
                break

    # Always inspect the newest page first so new activity is not hidden while
    # an older, multi-page checkpoint backfill is being resumed.
    save_page(first_page)
    pages_this_call = 0
    pages_scanned_total = int(progress.get("pages_scanned", 0)) if progress else 0
    cursor = (
        progress.get("next_before_signature")
        if progress else (
            next(
                (
                    item.get("signature")
                    for item in reversed(first_page)
                    if isinstance(item, dict)
                    and is_valid_transaction_signature(item.get("signature"))
                ),
                None,
            )
        )
    )
    seen_cursors = {cursor} if isinstance(cursor, str) else set()
    scan_error = None
    complete = False

    if anchor_found:
        complete = True
    elif baseline_mode and not first_page:
        complete = True
    elif not baseline_mode and anchor_signature is None:
        # Older stores without a usable checkpoint start from the current page;
        # never walk an entire wallet history without a known starting point.
        complete = True
    elif progress is None and baseline_mode and len(first_page) < limit:
        complete = True
    elif progress is None and not baseline_mode and len(first_page) < limit:
        scan_error = (
            "The saved wallet checkpoint was not found before the available history ended."
        )

    # For an incomplete scan, resume immediately before the last fully saved
    # page. The cursor is persisted so repeated cycles make forward progress.
    while not complete and scan_error is None and pages_this_call < max_pages:
        if not is_valid_transaction_signature(cursor):
            scan_error = "Wallet pagination returned no valid continuation cursor."
            break
        page_options = {
            "wallet_address": wallet_address,
            "limit": limit,
            "rpc_url": rpc_url,
            "timeout": timeout,
            "before": cursor,
        }
        if latency_recorder is not None:
            page_options["latency_recorder"] = latency_recorder
        page = get_wallet_signatures(**page_options)
        pages_this_call += 1
        if not isinstance(page, list):
            scan_error = "Wallet pagination RPC did not return a list."
            break
        if not page:
            if baseline_mode:
                complete = True
            else:
                scan_error = (
                    "The saved wallet checkpoint could not be reached; the scan remains incomplete."
                )
            break

        save_page(page)
        if anchor_found:
            complete = True
            break
        if len(page) < limit:
            if baseline_mode:
                complete = True
            else:
                scan_error = (
                    "The saved wallet checkpoint was not found before the available history ended."
                )
            break

        next_cursor = next(
            (
                item.get("signature")
                for item in reversed(page)
                if isinstance(item, dict)
                and is_valid_transaction_signature(item.get("signature"))
            ),
            None,
        )
        if not is_valid_transaction_signature(next_cursor) or next_cursor in seen_cursors:
            scan_error = "Wallet pagination returned a repeated or invalid cursor."
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    pages_scanned_total += pages_this_call
    if complete:
        store.clear_scan_progress(wallet_address)
        if baseline_mode:
            store.mark_baseline(wallet_address)
        # All signatures between this observed head and the checkpoint/end of
        # history are now durably saved. Only now may the checkpoint advance.
        if newest_record is not None:
            newest_slot = newest_record.get("slot")
            newest_signature = newest_record.get("signature")
            if (
                isinstance(newest_slot, int)
                and not isinstance(newest_slot, bool)
                and newest_slot >= 0
                and is_valid_transaction_signature(newest_signature)
            ):
                store.set_position(wallet_address, newest_slot, newest_signature)
    else:
        if is_valid_transaction_signature(cursor):
            store.save_scan_progress(
                wallet_address,
                mode=mode,
                anchor_signature=anchor_signature,
                newest_signature=(
                    newest_record.get("signature") if newest_record else
                    (progress.get("newest_signature") if progress else None)
                ),
                newest_slot=(
                    newest_record.get("slot") if newest_record else
                    (progress.get("newest_slot") if progress else None)
                ),
                next_before_signature=cursor,
                pages_scanned=pages_scanned_total,
                error=scan_error,
            )

    pending = store.get_pending_signatures(wallet_address, limit=limit)
    return [
        {
            **records_by_signature.get(signature, {"signature": signature}),
            **(
                {"historical_baseline": True}
                if store.is_historical_baseline(wallet_address, signature)
                else {}
            ),
        }
        for signature in pending
    ]

if __name__ == "__main__":
    print("Read-only wallet monitor loaded successfully.")
