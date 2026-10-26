
"""Read-only access to public Solana RPC data. No keys or transactions."""

import json
from engine.rpc_policy import post_json_rpc


DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"


class SolanaRPCError(Exception):
    """Raised when a public RPC request fails."""


def _rpc_request(payload, rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Send a read-only JSON-RPC request and validate its response."""

    try:
        data = post_json_rpc(payload, rpc_url=rpc_url, timeout=timeout)
    except RuntimeError as exc:
        raise SolanaRPCError(str(exc)) from None

    if not isinstance(data, dict):
        raise SolanaRPCError("Solana RPC returned an invalid response.")

    if "error" in data:
        raise SolanaRPCError("Solana RPC returned an error.")

    return data.get("result")


def get_public_health(rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Check the health of the public Solana RPC endpoint."""

    return _rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
            "params": [],
        },
        rpc_url=rpc_url,
        timeout=timeout,
    )


def get_slot(rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Get the current Solana slot."""

    return _rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "getSlot",
            "params": [],
        },
        rpc_url=rpc_url,
        timeout=timeout,
    )


def get_latest_blockhash(rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Read the latest blockhash. This function does not submit transactions."""

    result = _rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "getLatestBlockhash",
            "params": [],
        },
        rpc_url=rpc_url,
        timeout=timeout,
    )

    if not isinstance(result, dict):
        raise SolanaRPCError(
            "Solana RPC returned invalid blockhash data."
        )

    value = result.get("value")

    if not isinstance(value, dict):
        raise SolanaRPCError(
            "Solana RPC returned invalid blockhash data."
        )

    blockhash = value.get("blockhash")

    if not isinstance(blockhash, str) or not blockhash:
        raise SolanaRPCError(
            "Solana RPC response did not contain a blockhash."
        )

    return blockhash


def get_transaction(signature, rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Fetch a transaction by signature using public Solana RPC."""

    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("A transaction signature is required.")

    result = _rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "json",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        },
        rpc_url=rpc_url,
        timeout=timeout,
    )

    if result is not None and not isinstance(result, dict):
        raise SolanaRPCError(
            "Solana RPC returned invalid transaction data."
        )

    return result


if __name__ == "__main__":
    print("Read-only Solana RPC module loaded successfully.")
