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
    """Fetch and analyze a transaction without executing any trades.

    When supplied, latency_recorder receives separate RPC-fetch and local
    analysis timings. These timings do not represent on-chain execution.
    """

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
            "error": "Transaction data is not available yet.",
        }

    if not isinstance(transaction, Mapping):
        raise SolanaRPCError(
            "Transaction data returned by RPC is invalid."
        )

    metadata = transaction.get("meta")

    if not isinstance(metadata, Mapping):
        raise SolanaRPCError(
            "Transaction data returned by RPC has invalid metadata."
        )

    try:
        analysis_timer = (
            latency_recorder.measure("transaction.local_analysis")
            if latency_recorder is not None
            else nullcontext()
        )
        with analysis_timer:
            analysis = analyze_transaction(transaction)
            token_balance_changes = analyze_token_balance_changes(metadata)

            # Include native SOL movements only for successful transactions.
            # Failed transactions must never become buy/sell candidates.
            if analysis.get("status") == "success":
                token_balance_changes.extend(
                    analyze_native_sol_balance_changes(transaction)
                )

    except ValueError as exc:
        raise SolanaRPCError(
            "Transaction data could not be analyzed."
        ) from exc

    return {
        "signature": signature,
        **analysis,
        "token_balance_changes": token_balance_changes,
    }


if __name__ == "__main__":
    print("Read-only transaction processor loaded successfully.")