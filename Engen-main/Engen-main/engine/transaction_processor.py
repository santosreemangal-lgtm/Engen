"""Read-only processing of public Solana transactions."""

from collections.abc import Mapping
from contextlib import nullcontext

from engine.solana_public import (
    DEFAULT_RPC_URL,
    SolanaRPCError,
    get_transaction,
)
from engine.transaction_analysis import (
    analyze_transaction,
    analyze_token_balance_changes,
    analyze_native_sol_balance_changes,
)


def process_transaction_signature(
    signature,
    rpc_url=DEFAULT_RPC_URL,
    timeout=10,
    latency_recorder=None,
):
    """Fetch and analyze a transaction; incomplete provider data stays explicit."""
    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("A transaction signature is required.")

    rpc_timer = (
        latency_recorder.measure("transaction.rpc_fetch")
        if latency_recorder is not None
        else nullcontext()
    )
    with rpc_timer:
        transaction = get_transaction(
            signature=signature,
            rpc_url=rpc_url,
            timeout=timeout,
        )

    if transaction is None:
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "not_available_yet",
            "error": "Transaction data is not available yet.",
        }

    if not isinstance(transaction, Mapping):
        raise SolanaRPCError("Transaction data returned by RPC is invalid.")

    slot = transaction.get("slot")
    if slot is not None and (
        not isinstance(slot, int) or isinstance(slot, bool) or slot < 0
    ):
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "invalid_slot",
            "error": "Transaction slot metadata is invalid.",
        }

    metadata = transaction.get("meta")
    if metadata is None:
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "missing",
            "slot": slot,
            "error": "Transaction metadata is unavailable.",
        }
    if not isinstance(metadata, Mapping):
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "invalid",
            "slot": slot,
            "error": "Transaction metadata is invalid.",
        }

    fee = metadata.get("fee")
    if fee is not None and (
        not isinstance(fee, int) or isinstance(fee, bool) or fee < 0
    ):
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "invalid_fee",
            "slot": slot,
            "error": "Transaction fee metadata is invalid.",
        }

    try:
        analysis_timer = (
            latency_recorder.measure("transaction.local_analysis")
            if latency_recorder is not None
            else nullcontext()
        )
        with analysis_timer:
            analysis = analyze_transaction(transaction)
            token_balance_changes = analyze_token_balance_changes(metadata)
            # Failed transactions may pay fees but cannot become swap candidates.
            if analysis.get("status") == "success":
                token_balance_changes.extend(
                    analyze_native_sol_balance_changes(transaction)
                )
    except ValueError:
        raise SolanaRPCError(
            "Transaction data could not be analyzed."
        ) from None

    required_metadata = (
        "err", "fee", "preBalances", "postBalances",
        "preTokenBalances", "postTokenBalances",
    )
    missing_fields = [
        field for field in required_metadata if field not in metadata
    ]
    return {
        "signature": signature,
        **analysis,
        "available": True,
        "metadata_complete": not missing_fields,
        "metadata_missing_fields": missing_fields,
        "token_balance_changes": token_balance_changes,
    }


if __name__ == "__main__":
    print("Read-only transaction processor loaded successfully.")
