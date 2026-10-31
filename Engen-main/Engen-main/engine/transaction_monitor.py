"""Monitor public Solana wallet transactions without submitting trades."""

from concurrent.futures import ThreadPoolExecutor

from engine.solana_public import DEFAULT_RPC_URL, SolanaRPCError, get_signature_status
from engine.signature_store import SignatureStore
from engine.solana_validation import is_valid_solana_address, is_valid_transaction_signature
from engine.transaction_analysis import classify_transaction
from engine.transaction_processor import process_transaction_signature
from engine.wallet_monitor import get_and_store_new_wallet_signatures


def _include_signature_observation(result, item):
    """Attach provider signature-list observations without treating them as proof."""
    fields = {
        "slot": item.get("slot"),
        "confirmation_status": item.get("confirmationStatus"),
        "signature_error": item.get("err"),
        "block_time": item.get("blockTime"),
    }
    if any(value is not None for value in fields.values()):
        result = {**result, "signature_observation": fields}
    if item.get("historical_baseline") is True:
        result = {**result, "historical_baseline": True}
    return result


def _unknown_signer_signal():
    """Return a conservative signal when signer ownership is unconfirmed."""
    return {
        "classification": "UNKNOWN",
        "reason": (
            "The monitored wallet is not confirmed as a transaction signer."
        ),
        "asset_mints": [],
        "quote_mints": [],
        "confidence": "low",
        "evidence": {
            "classification_basis": "signer ownership could not be confirmed",
            "automatic_trade_allowed": False,
        },
    }


def process_new_signatures(
    signatures,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    wallet_address=None,
    store=None,
    defer_processed=False,
    latency_recorder=None,
):
    """Analyze signatures and update persistent processing status."""

    if not isinstance(signatures, list):
        raise ValueError("Signatures must be provided as a list.")

    results = []

    # Fetch transaction payloads concurrently, then apply all SignatureStore
    # mutations in the original order on this calling thread. getTransaction
    # accepts one signature per RPC request, so this overlaps bounded requests
    # rather than relying on provider-specific JSON-RPC batch support.
    fetch_futures = {}
    fetchable = [
        (index, item) for index, item in enumerate(signatures)
        if isinstance(item, dict)
        and isinstance(item.get("signature"), str)
        and item.get("signature", "").strip()
    ]
    if fetchable:
        with ThreadPoolExecutor(
            max_workers=min(4, len(fetchable)),
            thread_name_prefix="solana-transaction-fetch",
        ) as executor:
            for index, item in fetchable:
                signature = item["signature"]
                processor_options = {
                    "signature": signature,
                    "rpc_url": rpc_url,
                    "timeout": timeout,
                }
                if latency_recorder is not None:
                    processor_options["latency_recorder"] = latency_recorder
                fetch_futures[index] = executor.submit(
                    process_transaction_signature, **processor_options
                )

    for index, item in enumerate(signatures):
        if not isinstance(item, dict):
            continue

        signature = item.get("signature")

        if not isinstance(signature, str) or not signature.strip():
            continue

        try:
            result = fetch_futures[index].result()
        except ValueError:
            # Invalid signatures are rejected before any transaction RPC call.
            results.append(_include_signature_observation({
                "signature": signature,
                "available": False,
                "metadata_complete": False,
                "metadata_status": "invalid_signature",
                "error": "Invalid transaction signature; RPC request was not sent.",
                "retryable": False,
            }, item))
            continue
        except SolanaRPCError as exc:
            provider_code = getattr(exc, "code", None)
            error_result = {
                "signature": signature,
                "available": False,
                "metadata_complete": False,
                "metadata_status": "rpc_error",
                "error": str(exc),
                "retryable": True,
            }
            if isinstance(provider_code, int) and not isinstance(provider_code, bool):
                error_result["rpc_code"] = provider_code
            if (
                wallet_address is not None
                and store is not None
                and is_valid_transaction_signature(signature)
            ):
                failure_code = (
                    "rpc_provider_" + str(provider_code)
                    if isinstance(provider_code, int) and not isinstance(provider_code, bool)
                    else type(exc).__name__
                )
                failure = store.record_failure(
                    wallet_address, signature, "rpc_fetch",
                    failure_code, str(exc),
                )
                error_result["processing_status"] = failure["status"]
                if failure["status"] == "needs_review":
                    error_result["retryable"] = False
            results.append(_include_signature_observation(error_result, item))
            continue
        except Exception as exc:
            error_result = {
                "signature": signature,
                "available": False,
                "metadata_complete": False,
                "metadata_status": "interpretation_error",
                "error": "Transaction interpretation failed; retry scheduled.",
                "retryable": True,
            }
            if (
                wallet_address is not None
                and store is not None
                and is_valid_transaction_signature(signature)
            ):
                failure = store.record_failure(
                    wallet_address, signature, "interpretation",
                    type(exc).__name__, "Transaction interpretation failed.",
                )
                error_result["processing_status"] = failure["status"]
                if failure["status"] == "needs_review":
                    error_result["retryable"] = False
            results.append(_include_signature_observation(error_result, item))
            continue

        if (
            result.get("available") is False
            or result.get("metadata_complete") is False
        ):
            pending_result = {
                **result,
                "available": False,
                "retryable": True,
            }
            if result.get("metadata_complete") is False:
                pending_result["metadata_status"] = result.get(
                    "metadata_status", "incomplete_metadata"
                )
                pending_result["error"] = result.get(
                    "error", "Transaction metadata is incomplete; retrying."
                )
            if (
                wallet_address is not None
                and store is not None
                and is_valid_transaction_signature(signature)
            ):
                failure = store.record_failure(
                    wallet_address, signature, "rpc_fetch",
                    result.get("metadata_status", "incomplete_metadata"),
                    result.get("error", "Transaction data is incomplete."),
                )
                pending_result["processing_status"] = failure["status"]
                if result.get("raw_transaction") is not None:
                    store.save_transaction_record(
                        wallet_address,
                        signature,
                        result.get("raw_transaction"),
                        processing_status=(
                            "needs_review" if failure["status"] == "needs_review"
                            else "incomplete"
                        ),
                    )
                if failure["status"] == "needs_review":
                    pending_result["retryable"] = False
            results.append(_include_signature_observation(pending_result, item))
            continue

        # Some RPC providers omit confirmationStatus from getSignaturesForAddress.
        # Resolve missing/processed observations with a separate, read-only status
        # lookup. If the lookup fails or remains unconfirmed, stay conservative.
        if (
            wallet_address is not None
            and is_valid_solana_address(wallet_address)
            and is_valid_transaction_signature(signature)
            and result.get("status") == "success"
            and item.get("confirmationStatus") not in ("confirmed", "finalized")
        ):
            try:
                status_timer = (
                    latency_recorder.measure("transaction.signature_status_rpc")
                    if latency_recorder is not None
                    else nullcontext()
                )
                with status_timer:
                    resolved_status = get_signature_status(
                        signature, rpc_url=rpc_url, timeout=timeout
                    )
            except SolanaRPCError:
                resolved_status = None
            if isinstance(resolved_status, dict):
                item = {
                    **item,
                    "confirmationStatus": resolved_status.get("confirmationStatus"),
                }
                if item.get("slot") is None and resolved_status.get("slot") is not None:
                    item["slot"] = resolved_status["slot"]
                if item.get("err") is None and resolved_status.get("err") is not None:
                    item["err"] = resolved_status["err"]

        # getTransaction was requested at confirmed commitment. A complete,
        # successful transaction returned by that RPC is itself evidence that
        # the transaction reached the requested commitment. Some providers
        # omit or fail to return getSignatureStatuses history for older
        # signatures; do not leave such transactions pending indefinitely.
        if (
            result.get("available") is True
            and result.get("metadata_complete") is True
            and result.get("status") == "success"
            and item.get("confirmationStatus") not in ("confirmed", "finalized")
        ):
            item = {
                **item,
                "confirmationStatus": "confirmed",
            }

        # Preserve provider observation metadata separately from transaction
        # metadata; a signature-list observation is not itself transaction proof.
        result = _include_signature_observation(result, item)

        # Keep only token and native SOL changes attributed to the
        # monitored wallet. Unknown owners are not attributed to it.
        if wallet_address is not None:
            changes = result.get("token_balance_changes")

            if isinstance(changes, list):
                result = {
                    **result,
                    "token_balance_changes": [
                        change
                        for change in changes
                        if isinstance(change, dict)
                        and change.get("owner") == wallet_address
                    ],
                }

        # A balance change alone does not prove that the monitored wallet
        # initiated a transaction. Classify only when signer information is
        # available and confirms the monitored wallet as a signer.
        if (
            wallet_address is not None
            and result.get("status") in ("success", "failed")
            and isinstance(result.get("token_balance_changes"), list)
        ):
            signers = result.get("signers")
            confirmation = item.get("confirmationStatus")

            if result.get("status") == "failed":
                result = {
                    **result,
                    "trade_signal": classify_transaction(
                        result.get("raw_transaction"),
                        wallet_address,
                        result["token_balance_changes"],
                    ),
                }
            elif item.get("err") is not None:
                result = {
                    **result,
                    "trade_signal": {**_unknown_signer_signal(),
                        "reason": (
                            "Signature status reports a transaction error; "
                            "no buy/sell candidate was created."
                        ),
                    },
                    "confirmation_sufficient": False,
                    "retryable": True,
                }
            elif confirmation not in ("confirmed", "finalized"):
                result = {
                    **result,
                    "trade_signal": {**_unknown_signer_signal(),
                        "reason": (
                            "Transaction confirmation is insufficient; "
                            "no buy/sell candidate was created."
                        ),
                    },
                    "confirmation_sufficient": False,
                    "retryable": True,
                }
            elif (
                not isinstance(signers, list)
                or wallet_address not in signers
            ):
                result = {
                    **result,
                    "trade_signal": _unknown_signer_signal(),
                }
            else:
                result = {
                    **result,
                    "trade_signal": classify_transaction(
                        result.get("raw_transaction"),
                        wallet_address,
                        result["token_balance_changes"],
                    ),
                }

        if (
            wallet_address is not None
            and store is not None
            and is_valid_transaction_signature(signature)
        ):
            record_status = (
                "incomplete" if result.get("metadata_complete") is False
                else "failed" if result.get("status") == "failed"
                else "observed"
            )
            store.save_transaction_record(
                wallet_address,
                signature,
                result.get("raw_transaction"),
                processing_status=record_status,
            )

        # When deferred, the paper-trading runner is responsible for
        # marking the signature processed after handling the result.
        if (
            wallet_address is not None
            and store is not None
            and not defer_processed
        ):
            if result.get("retryable") is not True:
                store.mark_processed(wallet_address, signature)

        results.append(result)

    return results


def monitor_wallet(
    wallet_address,
    store=None,
    limit=10,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    defer_processed=False,
    latency_recorder=None,
):
    """Monitor a public wallet and retry pending transactions."""

    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A public wallet address is required.")

    if store is None:
        store = SignatureStore()

    signature_options = {
        "wallet_address": wallet_address,
        "store": store,
        "limit": limit,
        "rpc_url": rpc_url,
        "timeout": timeout,
    }
    if latency_recorder is not None:
        signature_options["latency_recorder"] = latency_recorder
    pending_signatures = get_and_store_new_wallet_signatures(**signature_options)

    monitor_options = {
        "signatures": pending_signatures,
        "rpc_url": rpc_url,
        "timeout": timeout,
        "wallet_address": wallet_address,
        "store": store,
        "defer_processed": defer_processed,
    }
    if latency_recorder is not None:
        monitor_options["latency_recorder"] = latency_recorder
    return process_new_signatures(**monitor_options)


if __name__ == "__main__":
    print("Read-only transaction monitor loaded successfully.")