"""Validated, read-only access to public Solana JSON-RPC data."""

from engine.rpc_policy import (
    RPCInvalidResponseError,
    RPCHTTPError,
    RPCTransportError,
    post_json_rpc,
)


DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class SolanaRPCError(Exception):
    """Base error for safe, normalized Solana RPC failures."""


class SolanaRPCHTTPError(SolanaRPCError):
    """HTTP error distinct from a JSON-RPC provider error."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__("Public Solana RPC HTTP request failed.")


class SolanaRPCTransportError(SolanaRPCError):
    """Timeout or temporary network failure after bounded retries."""


class SolanaRPCProviderError(SolanaRPCError):
    """The provider returned a JSON-RPC error object."""

    def __init__(self, code=None):
        self.code = code
        super().__init__("Solana RPC returned an error response.")


def is_valid_transaction_signature(signature):
    """Return True only for a base58-encoded 64-byte transaction signature."""
    if not isinstance(signature, str) or not signature:
        return False
    if any(char not in _BASE58_ALPHABET for char in signature):
        return False

    number = 0
    for char in signature:
        number = number * 58 + _BASE58_ALPHABET.index(char)

    decoded = bytearray()
    while number:
        number, remainder = divmod(number, 256)
        decoded.append(remainder)
    decoded.reverse()
    leading_ones = len(signature) - len(signature.lstrip("1"))
    decoded = bytearray(leading_ones) + decoded
    return len(decoded) == 64


def _rpc_request(payload, rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Send one read-only JSON-RPC request and validate its envelope."""
    try:
        data = post_json_rpc(payload, rpc_url=rpc_url, timeout=timeout)
    except RPCHTTPError as exc:
        raise SolanaRPCHTTPError(exc.status_code) from None
    except RPCTransportError:
        raise SolanaRPCTransportError(
            "Public Solana RPC request failed."
        ) from None
    except RPCInvalidResponseError:
        raise SolanaRPCError(
            "Solana RPC returned an invalid response."
        ) from None
    except RuntimeError:
        raise SolanaRPCError(
            "Public Solana RPC request failed."
        ) from None

    if not isinstance(data, dict):
        raise SolanaRPCError("Solana RPC returned an invalid response.")

    if "jsonrpc" in data and data.get("jsonrpc") != "2.0":
        raise SolanaRPCError("Solana RPC returned an invalid response.")

    if "id" in data and data.get("id") != payload.get("id"):
        raise SolanaRPCError("Solana RPC returned a mismatched response ID.")

    if "error" in data:
        error = data.get("error")
        if not isinstance(error, dict):
            raise SolanaRPCError("Solana RPC returned an invalid error object.")
        code = error.get("code")
        raise SolanaRPCProviderError(
            code if isinstance(code, int) and not isinstance(code, bool) else None
        )

    if "result" not in data:
        raise SolanaRPCError("Solana RPC response is missing its result.")

    return data["result"]


def get_public_health(rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Check the health of the public Solana RPC endpoint."""
    result = _rpc_request(
        {"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []},
        rpc_url=rpc_url,
        timeout=timeout,
    )
    if not isinstance(result, str) or not result:
        raise SolanaRPCError("Solana RPC returned invalid health data.")
    return result


def get_slot(rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Get the current slot, rejecting malformed numeric responses."""
    result = _rpc_request(
        {"jsonrpc": "2.0", "id": 2, "method": "getSlot", "params": []},
        rpc_url=rpc_url,
        timeout=timeout,
    )
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise SolanaRPCError("Solana RPC returned invalid slot data.")
    return result


def get_latest_blockhash(rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Read the latest blockhash. This function never submits transactions."""
    result = _rpc_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "confirmed"}],
        },
        rpc_url=rpc_url,
        timeout=timeout,
    )
    value = result.get("value") if isinstance(result, dict) else None
    blockhash = value.get("blockhash") if isinstance(value, dict) else None
    if not isinstance(blockhash, str) or not blockhash:
        raise SolanaRPCError("Solana RPC returned invalid blockhash data.")
    return blockhash


def get_transaction(signature, rpc_url=DEFAULT_RPC_URL, timeout=10):
    """Fetch one transaction after validating its base58 signature."""
    if not is_valid_transaction_signature(signature):
        raise ValueError("A valid 64-byte base58 transaction signature is required.")

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
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        },
        rpc_url=rpc_url,
        timeout=timeout,
    )

    if result is not None and not isinstance(result, dict):
        raise SolanaRPCError("Solana RPC returned invalid transaction data.")
    return result


if __name__ == "__main__":
    print("Read-only Solana RPC module loaded successfully.")
