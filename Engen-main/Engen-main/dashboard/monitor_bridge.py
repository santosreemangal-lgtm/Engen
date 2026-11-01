
"""Bridge between the dashboard and the read-only Solana engine."""

import sqlite3
from decimal import Decimal, InvalidOperation

from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet
from engine.paper_copy_runner import (queue_paper_orders, execute_pending_paper_orders,
    get_paper_simulator, PAPER_SIMULATOR_LOCK, source_trade_is_fresh)
from engine.profile_store import LABELS
from engine.solana_validation import is_valid_transaction_signature


def create_scan_function(limit=10, timeout=10, side_getter=None, settings_getter=None,
                         simulator_getter=None, profile_id=None, profile_store=None):
    """Create a read-only scanner with profile-specific paper execution and history."""
    store = SignatureStore()

    def persist_observed_transactions(address, results):
        """Save observed on-chain transactions separately from simulated orders."""
        if profile_id is None or profile_store is None:
            return results
        persisted = []
        for item in results:
            if not isinstance(item, dict):
                persisted.append(item)
                continue
            signature = item.get("signature")
            if not is_valid_transaction_signature(signature):
                persisted.append(item)
                continue
            observation = item.get("signature_observation")
            observation = observation if isinstance(observation, dict) else {}
            signal = item.get("trade_signal")
            signal = signal if isinstance(signal, dict) else {}
            classification = signal.get("classification", "UNKNOWN")
            if classification not in LABELS:
                classification = "UNKNOWN"
            confirmation = observation.get("confirmation_status")
            if item.get("status") == "failed":
                status = "failed"
            elif item.get("processing_status") == "needs_review":
                # Profile history has no needs_review enum. Map the exhausted
                # retry state to unknown, not pending, so the UI does not imply
                # the engine will keep retrying it automatically.
                status = "unknown"
            elif confirmation in ("confirmed", "finalized") and item.get("metadata_complete") is True:
                status = "confirmed"
            elif item.get("retryable") is True or item.get("metadata_complete") is False:
                status = "pending"
            elif item.get("status") == "success":
                status = "observed"
            else:
                status = "unknown"
            slot = observation.get("slot", item.get("slot"))
            if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
                slot = None
            block_time = observation.get("block_time", item.get("block_time"))
            if not isinstance(block_time, int) or isinstance(block_time, bool) or block_time < 0:
                block_time = None
            # The engine represents delta_amount in raw base units (integers).
            # Store that exact amount; the history API converts it using decimals.
            token_mint = None
            token_decimals = None
            raw_amount = None
            changes = item.get("token_balance_changes")
            if isinstance(changes, list):
                candidate_mints = signal.get("asset_mints")
                candidate_mints = candidate_mints if isinstance(candidate_mints, list) else []
                ordered_changes = sorted(
                    (change for change in changes if isinstance(change, dict)),
                    key=lambda change: 0 if change.get("mint") in candidate_mints else 1,
                )
                for change in ordered_changes:
                    mint = change.get("mint")
                    decimals = change.get("decimals")
                    raw_change = change.get("delta_amount")
                    if not isinstance(mint, str) or not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 30:
                        continue
                    if not isinstance(raw_change, str) or not (raw_change[1:].isdigit() if raw_change.startswith("-") else raw_change.isdigit()):
                        continue
                    token_mint = mint
                    token_decimals = decimals
                    raw_amount = str(abs(int(raw_change)))
                    break
            evidence = {
                "metadata_status": str(item.get("metadata_status", "available"))[:80],
                "processing_status": str(item.get("processing_status", "unknown"))[:40],
                "confirmation_status": confirmation,
                "classification_reason": str(signal.get("reason", ""))[:400],
                "confidence": str(signal.get("confidence", "unknown"))[:40],
                "monitored_wallet_is_signer": address in item.get("signers", []) if isinstance(item.get("signers"), list) else False,
                "real_trading": False,
                "transaction_broadcast": False,
            }
            try:
                profile_store.save_transaction(
                    signature, address, classification=classification, status=status,
                    evidence=evidence, slot=slot, block_time=block_time,
                    raw_amount=raw_amount, token_mint=token_mint, token_decimals=token_decimals,
                    profile_ids=(profile_id,),
                )
                persisted.append({**item, "profile_history_status": "saved"})
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                persisted.append({**item, "profile_history_status": "save_failed"})
        return persisted


    def reconcile_terminal_review_history(address):
        """Reconcile old profile-history pending rows whose signature retries are exhausted."""
        if profile_id is None or profile_store is None:
            return 0
        pending_reader = getattr(profile_store, "get_pending_transaction_signatures", None)
        review_writer = getattr(profile_store, "mark_transaction_needs_review", None)
        status_batch_reader = getattr(store, "get_statuses", None)
        status_reader = getattr(store, "get_status", None)
        error_reader = getattr(store, "get_error_record", None)
        if not all(callable(item) for item in (
            pending_reader, review_writer, error_reader
        )) or not (callable(status_batch_reader) or callable(status_reader)):
            return 0
        try:
            signatures = pending_reader(address, limit=1000)
        except (TypeError, ValueError, OSError, RuntimeError, sqlite3.Error):
            return 0
        if not isinstance(signatures, (list, tuple)):
            return 0
        reconciled = 0
        statuses = None
        if callable(status_batch_reader):
            try:
                candidate_statuses = status_batch_reader(address, signatures)
                if isinstance(candidate_statuses, dict):
                    statuses = candidate_statuses
            except (TypeError, ValueError, OSError, RuntimeError, sqlite3.Error):
                statuses = None
        for signature in signatures:
            try:
                signature_status = (
                    statuses.get(signature) if statuses is not None
                    else status_reader(address, signature)
                )
                if signature_status != "needs_review":
                    continue
                error = error_reader(address, signature) or {}
                changed = review_writer(
                    signature, address,
                    attempts=error.get("attempts"),
                    error_stage=error.get("error_stage"),
                    error_code=error.get("error_code"),
                    error_message=error.get("error_message"),
                )
                reconciled += int(bool(changed))
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                # Revisit this row on a later scan; reconciliation is idempotent.
                continue
        return reconciled


    def persist_source_trade_refs(address, simulator):
        """Reconcile paper orders into the durable profile source-reference ledger."""
        if profile_id is None or profile_store is None:
            return 0
        reporter = getattr(simulator, "report", None)
        if not callable(reporter):
            return 0
        try:
            report = reporter()
        except Exception:
            return 0
        orders = list(report.get("orders", [])) if isinstance(report, dict) and isinstance(report.get("orders"), list) else []
        archives = report.get("archived_sessions", []) if isinstance(report, dict) else []
        # Session resets archive orders before clearing the active simulator list.
        # Reconcile those orders too, so a restart/reset cannot erase the source link.
        if isinstance(archives, list):
            for archive in archives:
                if isinstance(archive, dict) and isinstance(archive.get("orders"), list):
                    orders.extend(archive["orders"])
        saved = 0
        for order in orders:
            if not isinstance(order, dict):
                continue
            details = order.get("details")
            details = details if isinstance(details, dict) else {}
            if details.get("origin") != "AUTOMATIC_WALLET_COPY":
                continue
            signature = details.get("source_signature")
            order_id = order.get("order_id")
            side = order.get("side")
            asset_mint = order.get("asset_mint")
            if not is_valid_transaction_signature(signature):
                continue
            try:
                created = profile_store.save_source_trade_ref(
                    profile_id, order_id, details.get("source_wallet", address),
                    signature, side, asset_mint,
                    source_slot=details.get("source_slot"),
                    source_block_time=details.get("source_block_time"),
                )
                saved += int(bool(created))
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                # The order is already safely persisted in the paper ledger.
                # A later scan reconciles it again; never lose that order due
                # to a transient source-reference write failure.
                continue
        return saved


    def scan_wallet(address):
        """Analyze new wallet signatures and queue eligible paper-copy orders."""
        results = monitor_wallet(
            wallet_address=address,
            store=store,
            limit=limit,
            timeout=timeout,
            defer_processed=True,
        )
        results = persist_observed_transactions(address, results)
        reconciled_review_rows = reconcile_terminal_review_history(address)
        if reconciled_review_rows:
            results = [
                {**item, "profile_review_rows_reconciled": reconciled_review_rows}
                if isinstance(item, dict) and index == 0 else item
                for index, item in enumerate(results)
            ]
        try:
            allowed_sides = side_getter() if side_getter is not None else "BOTH"
            copy_settings = settings_getter() if settings_getter is not None else {}
            simulator = simulator_getter() if simulator_getter is not None else get_paper_simulator()
            # Serialize order execution and balance writes against dashboard requests.
            with PAPER_SIMULATOR_LOCK:
                queued_orders = queue_paper_orders(
                    address, results, simulator=simulator,
                    allowed_sides=allowed_sides, copy_settings=copy_settings
                )
                execution_outcomes = execute_pending_paper_orders(address, simulator=simulator)
            # Reconcile the simulator's durable orders on every scan. This also
            # closes the crash window between order creation and reference write.
            persist_source_trade_refs(address, simulator)
            outcomes_by_signal = {
                item["signal_id"]: item for item in execution_outcomes
                if item.get("signal_id")
            }
            # Do not permanently consume a valid BUY/SELL candidate when
            # queueing failed. A later scan can retry after transient metadata,
            # settings, or balance-change issues are resolved. Orders already
            # queued (including deduplicated existing orders) are safe to mark.
            queued_signatures = {
                item.get("signature") for item in queued_orders
                if isinstance(item, dict) and isinstance(item.get("signature"), str)
            }
            allowed_mode = allowed_sides.upper() if isinstance(allowed_sides, str) else "NONE"
            processed_signals = getattr(simulator, "processed_signals", set())
            for item in results:
                signature = item.get("signature")
                if not isinstance(signature, str) or item.get("retryable") is True:
                    continue
                signal = item.get("trade_signal")
                classification = signal.get("classification") if isinstance(signal, dict) else None
                candidate_side = (
                    "BUY" if classification == "BUY_CANDIDATE"
                    else "SELL" if classification == "SELL_CANDIDATE"
                    else None
                )
                side_enabled = (
                    candidate_side is not None
                    and allowed_mode in (candidate_side, "BOTH")
                )
                signal_id = "wallet-copy:" + address + ":" + signature
                already_queued = (
                    signature in queued_signatures
                    or signal_id in processed_signals
                )
                if item.get("processing_status") == "needs_review":
                    item["paper_copy_status"] = "NEEDS_REVIEW"
                    item["paper_copy_message"] = (
                        "Automatic retries are exhausted; this signature needs manual review."
                    )
                    # record_failure() already moved this checkpoint to its
                    # terminal needs_review state; do not overwrite it as processed.
                    continue
                if side_enabled and not already_queued:
                    if not source_trade_is_fresh(item):
                        item["paper_copy_status"] = "SKIPPED_STALE_SOURCE"
                        item["paper_copy_message"] = (
                            "Source block time is missing or older than 60 seconds; no paper order was created."
                        )
                        store.mark_processed(address, signature)
                        continue
                    item["paper_copy_status"] = "RETRYING_QUEUE"
                    item["paper_copy_message"] = (
                        "Eligible swap was not queued; signature remains pending for retry."
                    )
                    continue
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

    # MonitorService reads this hook after each scan to expose durable per-wallet
    # backlog size, age, retry pressure, and net drain rate in its status API.
    metrics_function = getattr(store, "get_backlog_stats", None)
    if callable(metrics_function):
        scan_wallet.metrics_function = metrics_function
    return scan_wallet
