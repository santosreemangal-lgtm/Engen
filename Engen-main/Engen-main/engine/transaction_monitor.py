"""Monitor public Solana wallet transactions without submitting trades."""

from engine.solana_public import DEFAULT_RPC_URL, SolanaRPCError
from engine.signature_store import SignatureStore
from engine.transaction_analysis import classify_token_balance_changes
from engine.transaction_processor import process_transaction_signature
from engine.wallet_monitor import get_and_store_new_wallet_signatures


def _unknown_signer_signal():
    """Return a conservative signal when signer ownership is unconfirmed."""
    return {
        "classification": "UNKNOWN",
        "reason": (
            "The monitored wallet is not confirmed as a transaction signer."
        ),
        "asset_mints": [],
        "quote_mints": [],
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

    for item in signatures:
        if not isinstance(item, dict):
            continue

        signature = item.get("signature")

        if not isinstance(signature, str) or not signature.strip():
            continue

        try:
            processor_options = {
                "signature": signature,
                "rpc_url": rpc_url,
                "timeout": timeout,
            }
            if latency_recorder is not None:
                processor_options["latency_recorder"] = latency_recorder
            result = process_transaction_signature(**processor_options)
        except SolanaRPCError as exc:
            results.append({
                "signature": signature,
                "available": False,
                "error": str(exc),
                "retryable": True,
            })
            continue

        if result.get("available") is False:
            results.append({
                **result,
                "retryable": True,
            })
            continue

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
            and result.get("status") == "success"
            and isinstance(result.get("token_balance_changes"), list)
        ):
            signers = result.get("signers")

            if (
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
                    "trade_signal": classify_token_balance_changes(
                        result["token_balance_changes"],
                        wallet_address,
                    ),
                }

        # When deferred, the paper-trading runner is responsible for
        # marking the signature processed after handling the result.
        if (
            wallet_address is not None
            and store is not None
            and not defer_processed
        ):
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