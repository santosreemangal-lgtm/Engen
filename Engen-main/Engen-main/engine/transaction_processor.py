"""Read-only processing of public Solana transactions."""

from collections.abc import Mapping
from contextlib import nullcontext

from engine.solana_public import (
    DEFAULT_RPC_URL,
    SolanaRPCError,
    get_transaction,
)
from engine.solana_validation import is_valid_transaction_signature
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
    if not is_valid_transaction_signature(signature):
        raise ValueError("Invalid Solana transaction signature.")

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
            "raw_transaction": transaction,
        }

    transaction_data = transaction.get("transaction")
    message_data = (
        transaction_data.get("message")
        if isinstance(transaction_data, Mapping)
        else None
    )
    account_keys = (
        message_data.get("accountKeys")
        if isinstance(message_data, Mapping)
        else None
    )
    if not isinstance(account_keys, list):
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "incomplete_transaction",
            "slot": slot,
            "error": "Transaction message or account keys are unavailable.",
            "raw_transaction": transaction,
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
            "raw_transaction": transaction,
        }
    if not isinstance(metadata, Mapping):
        return {
            "signature": signature,
            "available": False,
            "metadata_complete": False,
            "metadata_status": "invalid",
            "slot": slot,
            "error": "Transaction metadata is invalid.",
            "raw_transaction": transaction,
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
            "raw_transaction": transaction,
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
        "raw_transaction": transaction,
    }


if __name__ == "__main__":
    print("Read-only transaction processor loaded successfully.")
