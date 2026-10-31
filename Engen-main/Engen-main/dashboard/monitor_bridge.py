
"""Bridge between the dashboard and the read-only Solana engine."""

import re
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from time import perf_counter_ns

from engine.signature_store import SignatureStore
from engine.latency import LatencyRecorder
from engine.market_data import fetch_market_price
from engine.transaction_monitor import monitor_wallet
from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders, get_paper_simulator, PAPER_SIMULATOR_LOCK
from engine.profile_store import LABELS
from engine.solana_validation import is_valid_transaction_signature


@contextmanager
def _measured_paper_lock(lock, latency_recorder, stage):
    """Measure lock wait separately from the short, mutation-only critical section."""
    waiting_since = perf_counter_ns()
    lock.acquire()
    latency_recorder.record(
        "dashboard.paper_lock_wait",
        (perf_counter_ns() - waiting_since) / 1_000_000,
    )
    try:
        with latency_recorder.measure(stage):
            yield
    finally:
        lock.release()


def create_scan_function(limit=10, timeout=10, side_getter=None, settings_getter=None,
                         simulator_getter=None, profile_id=None, profile_store=None,
                         latency_recorder=None, price_provider=None,
                         simulator_lock_getter=None, max_pages=100):
    """Create a measured read-only scanner with short paper-ledger lock sections."""
    store = SignatureStore()
    latency_recorder = latency_recorder or LatencyRecorder()
    price_provider = price_provider or fetch_market_price

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
            if item.get("processing_status") == "needs_review":
                # Keep the persisted status compatible with existing SQLite CHECK constraints.
                # The distinct review state is stored in evidence.processing_status.
                status = "pending"
            elif item.get("status") == "failed":
                status = "failed"
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
            # Never attach an arbitrary balance row to UNKNOWN/PENDING history.
            # The history amount is populated only for a confirmed, signer-owned
            # one-asset swap, and aggregates every SPL account row for that mint.
            token_mint = None
            token_decimals = None
            raw_amount = None
            token_amount_attribution = "unavailable_not_verified_swap"
            asset_mints = signal.get("asset_mints")
            quote_mints = signal.get("quote_mints")
            signers = item.get("signers")
            verified_swap = (
                classification in ("BUY_CANDIDATE", "SELL_CANDIDATE")
                and item.get("status") == "success"
                and item.get("metadata_complete") is True
                and item.get("retryable") is not True
                and confirmation in ("confirmed", "finalized")
                and isinstance(signers, list)
                and address in signers
                and isinstance(asset_mints, list)
                and len(asset_mints) == 1
                and isinstance(quote_mints, list)
                and len(quote_mints) == 1
                and isinstance(asset_mints[0], str)
                and isinstance(quote_mints[0], str)
                and asset_mints[0] != quote_mints[0]
            )
            changes = item.get("token_balance_changes")
            if verified_swap and isinstance(changes, list):
                asset_mint = asset_mints[0]
                matching_deltas = []
                decimals_seen = set()
                for change in changes:
                    if (
                        not isinstance(change, dict)
                        or change.get("owner") != address
                        or change.get("mint") != asset_mint
                        or change.get("source") == "native_sol"
                    ):
                        continue
                    decimals = change.get("decimals")
                    raw_change = change.get("delta_amount")
                    if (
                        not isinstance(decimals, int)
                        or isinstance(decimals, bool)
                        or not 0 <= decimals <= 30
                    ):
                        continue
                    if isinstance(raw_change, bool) or not isinstance(raw_change, (str, int)):
                        continue
                    raw_text = str(raw_change)
                    if re.fullmatch(r"-?\d+", raw_text) is None:
                        continue
                    decimals_seen.add(decimals)
                    matching_deltas.append(int(raw_text))
                if len(decimals_seen) == 1 and matching_deltas:
                    aggregate_delta = sum(matching_deltas)
                    direction_matches = (
                        aggregate_delta > 0
                        if classification == "BUY_CANDIDATE"
                        else aggregate_delta < 0
                    )
                    if direction_matches:
                        token_mint = asset_mint
                        token_decimals = next(iter(decimals_seen))
                        raw_amount = str(abs(aggregate_delta))
                        token_amount_attribution = "aggregated_wallet_owned_asset_rows"
            evidence = {
                "metadata_status": str(item.get("metadata_status", "available"))[:80],
                "processing_status": str(item.get("processing_status", "pending" if item.get("retryable") is True else "processed"))[:40],
                "rpc_code": item.get("rpc_code") if isinstance(item.get("rpc_code"), int) and not isinstance(item.get("rpc_code"), bool) else None,
                "confirmation_status": confirmation,
                "classification_reason": str(signal.get("reason", ""))[:400],
                "activity_type": str(signal.get("activity_type", classification))[:60],
                "confidence": str(signal.get("confidence", "unknown"))[:40],
                "historical_baseline": item.get("historical_baseline") is True,
                "monitored_wallet_is_signer": address in signers if isinstance(signers, list) else False,
                "token_amount_attribution": token_amount_attribution,
                "verified_swap_for_paper_queue": verified_swap,
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


    def _scan_wallet(address):
        """Analyze new wallet signatures and queue eligible paper-copy orders."""
        with latency_recorder.measure("dashboard.wallet_monitor_and_transaction_processing"):
            results = monitor_wallet(
                wallet_address=address,
                store=store,
                limit=limit,
                timeout=timeout,
                defer_processed=True,
                latency_recorder=latency_recorder,
                max_pages=max_pages,
            )
        pagination_incomplete = any(
            isinstance(item, dict) and item.get("scan_incomplete") is True
            for item in results
        )

        def mark_signature_processed(signature):
            if pagination_incomplete:
                store.mark_processed(address, signature, advance_position=False)
            else:
                store.mark_processed(address, signature,)

        with latency_recorder.measure("dashboard.transaction_history_persist"):
            results = persist_observed_transactions(address, results)
        try:
            allowed_sides = side_getter() if side_getter is not None else "BOTH"
            copy_settings = settings_getter() if settings_getter is not None else {}
            simulator = simulator_getter() if simulator_getter is not None else get_paper_simulator()
            simulator_lock = (
                simulator_lock_getter()
                if simulator_lock_getter is not None
                else PAPER_SIMULATOR_LOCK
            )

            # Queue mutations are protected, but public market-data requests are
            # deliberately performed after releasing the shared simulator lock.
            with _measured_paper_lock(simulator_lock, latency_recorder, "dashboard.paper_order_queue"):
                queued_orders = queue_paper_orders(
                    address, results, simulator=simulator,
                    allowed_sides=allowed_sides, copy_settings=copy_settings
                )

            stable_quote_mints = {
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            }
            with _measured_paper_lock(simulator_lock, latency_recorder, "dashboard.paper_order_snapshot"):
                pending_orders = [
                    dict(order, details=dict(order.get("details") or {}))
                    for order in simulator.orders.values()
                    if order.get("status") == "CREATED"
                    and (order.get("details") or {}).get("origin") == "AUTOMATIC_WALLET_COPY"
                    and (order.get("details") or {}).get("source_wallet") == address
                ]

            required_mints = set()
            for order in pending_orders:
                details = order.get("details") or {}
                asset_mint = order.get("asset_mint")
                if isinstance(asset_mint, str) and asset_mint.strip():
                    required_mints.add(asset_mint)
                if (
                    order.get("side") == "BUY"
                    and details.get("target_notional_usd") in (None, "")
                    and details.get("sizing_mode") == "MIRROR_AMOUNT"
                ):
                    source_quote_mint = details.get("source_quote_mint")
                    if (
                        isinstance(source_quote_mint, str)
                        and source_quote_mint.strip()
                        and source_quote_mint not in stable_quote_mints
                    ):
                        required_mints.add(source_quote_mint)

            market_prices = {}
            market_price_errors = {}
            with latency_recorder.measure("dashboard.paper_market_data_fetch"):
                if required_mints:
                    with ThreadPoolExecutor(
                        max_workers=min(4, len(required_mints)),
                        thread_name_prefix="paper-market-data",
                    ) as executor:
                        futures = {
                            mint: executor.submit(price_provider, mint)
                            for mint in sorted(required_mints)
                        }
                        for mint, future in futures.items():
                            try:
                                market_prices[mint] = future.result()
                            except Exception as exc:
                                market_price_errors[mint] = exc

            def prefetched_price(mint):
                if mint in market_price_errors:
                    raise market_price_errors[mint]
                return market_prices.get(mint)

            # Re-enter only for in-memory validation, simulator state changes and
            # SQLite persistence. No network I/O occurs while this lock is held.
            with _measured_paper_lock(simulator_lock, latency_recorder, "dashboard.paper_simulation"):
                execution_outcomes = execute_pending_paper_orders(
                    address, simulator=simulator, price_provider=prefetched_price
                )
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
            with _measured_paper_lock(simulator_lock, latency_recorder, "dashboard.paper_result_commit"):
                processed_signals = getattr(simulator, "processed_signals", set())
                for item in results:
                    signature = item.get("signature")
                    if (
                        not isinstance(signature, str)
                        or item.get("retryable") is True
                        or item.get("processing_status") == "needs_review"
                    ):
                        continue
                    if item.get("historical_baseline") is True:
                        # Historical observations are recorded in History, never copied.
                        mark_signature_processed(signature)
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
                    if side_enabled and not already_queued:
                        item["paper_copy_status"] = "RETRYING_QUEUE"
                        item["paper_copy_message"] = (
                            "Eligible swap was not queued; signature remains pending for retry."
                        )
                        continue
                    mark_signature_processed(signature)
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
                    and (not is_candidate or item.get("historical_baseline") is True)
                ):
                    mark_signature_processed(signature)
            results = [
                {**item, "paper_copy_error": "Paper-copy order queue failed."}
                for item in results
            ]
        return results

    def scan_wallet(address):
        with latency_recorder.measure("dashboard.wallet_scan_total"):
            return _scan_wallet(address)

    scan_wallet.latency_recorder = latency_recorder
    return scan_wallet
