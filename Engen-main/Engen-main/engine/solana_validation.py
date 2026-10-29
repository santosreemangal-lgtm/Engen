"""Strict validators for public Solana addresses and transaction signatures."""

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_ALPHABET)}


def decode_base58(value):
    """Decode a Base58 string, rejecting characters outside the alphabet."""
    if not isinstance(value, str) or not value:
        raise ValueError("Base58 value must be a non-empty string.")
    number = 0
    for character in value:
        if character not in _BASE58_INDEX:
            raise ValueError("Invalid Base58 value.")
        number = number * 58 + _BASE58_INDEX[character]
    decoded = (
        number.to_bytes((number.bit_length() + 7) // 8, "big")
        if number
        else b""
    )
    leading_ones = len(value) - len(value.lstrip("1"))
    return (b"\x00" * leading_ones) + decoded


def is_valid_solana_address(value):
    """Return whether value decodes to a 32-byte Solana public key."""
    try:
        return len(decode_base58(value)) == 32
    except (TypeError, ValueError):
        return False


def is_valid_transaction_signature(value):
    """Return whether value decodes to a 64-byte Solana signature."""
    try:
        return len(decode_base58(value)) == 64
    except (TypeError, ValueError):
        return False


def require_solana_address(value):
    if not is_valid_solana_address(value):
        raise ValueError("Invalid Solana public wallet address.")
    return value


def require_transaction_signature(value):
    if not is_valid_transaction_signature(value):
        raise ValueError("Invalid Solana transaction signature.")
    return value
