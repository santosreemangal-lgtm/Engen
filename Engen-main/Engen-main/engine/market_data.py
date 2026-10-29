"""Read-only public market snapshots for paper simulation.

Uses the documented DEX Screener token-pairs endpoint. These snapshots are
external estimates, not guaranteed executable quotes; the simulator still
applies liquidity, freshness, slippage, fee, and balance guards.
"""
import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engine.pnl import MarketPrice
from engine.solana_validation import is_valid_solana_address
from engine.transaction_analysis import WRAPPED_SOL_MINT


DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/"
USER_AGENT = "SolanaCopyTradingEngine-PaperSimulation/1.0"
MAX_RESPONSE_BYTES = 1_000_000
PRICE_CACHE_TTL_SECONDS = 5.0
_PRICE_CACHE = {}
_PRICE_CACHE_LOCK = threading.RLock()


def clear_market_price_cache():
    """Clear successful public-price snapshots; intended for deterministic tests."""
    with _PRICE_CACHE_LOCK:
        _PRICE_CACHE.clear()


def _positive_decimal(value):
    """Return a positive finite Decimal, or None for invalid public data."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def fetch_market_price(mint, timeout=8):
    """Return the highest-liquidity usable USD snapshot, or None.

    DEX Screener's priceUsd describes the pair's base token. If the requested
    mint is the quote token and that quote token is wrapped SOL, derive SOL's
    USD price from base priceUsd / base priceNative. Do not apply that formula
    to arbitrary quote tokens: priceNative is not guaranteed to be denominated
    in the requested quote token.
    """
    if not is_valid_solana_address(mint):
        return None
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("Market data timeout must be positive.")

    now_monotonic = time.monotonic()
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(mint)
        if cached is not None and cached[0] > now_monotonic:
            return cached[1]
        if cached is not None:
            _PRICE_CACHE.pop(mint, None)

    request = Request(
        DEXSCREENER_TOKEN_PAIRS_URL + mint,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, list):
        return None

    candidates = []
    for pair in payload:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue

        base = pair.get("baseToken")
        quote = pair.get("quoteToken")
        liquidity = pair.get("liquidity")
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        if not isinstance(liquidity, dict):
            continue

        base_address = base.get("address")
        quote_address = quote.get("address")
        price = None
        if base_address == mint:
            # priceUsd is the USD price of the base token.
            price = _positive_decimal(pair.get("priceUsd"))
        elif quote_address == mint and mint == WRAPPED_SOL_MINT:
            # For a SOL-quoted pool, priceNative is base tokens' price in SOL.
            # This ratio gives the implied USD price of one SOL.
            base_usd = _positive_decimal(pair.get("priceUsd"))
            base_in_sol = _positive_decimal(pair.get("priceNative"))
            if base_usd is not None and base_in_sol is not None:
                price = base_usd / base_in_sol

        liquidity_usd = _positive_decimal(liquidity.get("usd"))
        if price is None or liquidity_usd is None:
            continue
        candidates.append((liquidity_usd, price))

    if not candidates:
        return None

    liquidity_usd, price = max(candidates, key=lambda item: item[0])
    observed_at = datetime.now(timezone.utc).isoformat()
    snapshot = MarketPrice(
        mint=mint,
        price=price,
        currency="USD",
        source="dexscreener_public_api",
        observed_at=observed_at,
        liquidity_usd=liquidity_usd,
        liquidity_source="dexscreener_public_api",
        liquidity_observed_at=observed_at,
    )
    with _PRICE_CACHE_LOCK:
        _PRICE_CACHE[mint] = (time.monotonic() + PRICE_CACHE_TTL_SECONDS, snapshot)
    return snapshot
