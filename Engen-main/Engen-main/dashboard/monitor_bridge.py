
"""Bridge between the dashboard and the read-only Solana engine."""

import sqlite3
from decimal import Decimal, InvalidOperation

from engine.signature_store import SignatureStore
from engine.transaction_monitor import monitor_wallet
from engine.paper_copy_runner import queue_paper_orders, execute_pending_paper_orders, get_paper_simulator, PAPER_SIMULATOR_LOCK
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
                "processing_status": str(item.get("processing_status", "pending" if item.get("retryable") is True else "processed"))[:40],
                "rpc_code": item.get("rpc_code") if isinstance(item.get("rpc_code"), int) and not isinstance(item.get("rpc_code"), bool) else None,
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
                if (
                    not isinstance(signature, str)
                    or item.get("retryable") is True
                    or item.get("processing_status") == "needs_review"
                ):
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
