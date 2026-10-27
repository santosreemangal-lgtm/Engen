"""Read-only public market snapshots for paper simulation.

Uses the documented DEX Screener token-pairs endpoint. These snapshots are
external estimates, not guaranteed executable quotes; the simulator still
applies liquidity, freshness, slippage, fee, and balance guards.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engine.pnl import MarketPrice
from engine.solana_validation import is_valid_solana_address


DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/"
USER_AGENT = "SolanaCopyTradingEngine-PaperSimulation/1.0"


def fetch_market_price(mint, timeout=8):
    """Return the highest-liquidity usable Solana pool snapshot, or None."""
    if not is_valid_solana_address(mint):
        return None
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("Market data timeout must be positive.")

    request = Request(
        DEXSCREENER_TOKEN_PAIRS_URL + mint,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
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
        liquidity = pair.get("liquidity")
        if not isinstance(base, dict) or base.get("address") != mint:
            continue
        if not isinstance(liquidity, dict):
            continue
        try:
            price = Decimal(str(pair.get("priceUsd")))
            liquidity_usd = Decimal(str(liquidity.get("usd")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if (
            not price.is_finite() or price <= 0
            or not liquidity_usd.is_finite() or liquidity_usd < 0
        ):
            continue
        candidates.append((liquidity_usd, price, pair))

    if not candidates:
        return None

    liquidity_usd, price, pair = max(candidates, key=lambda item: item[0])
    observed_at = datetime.now(timezone.utc).isoformat()
    return MarketPrice(
        mint=mint,
        price=price,
        currency="USD",
        source="dexscreener_public_api",
        observed_at=observed_at,
        liquidity_usd=liquidity_usd,
        liquidity_source="dexscreener_public_api",
        liquidity_observed_at=observed_at,
    )
