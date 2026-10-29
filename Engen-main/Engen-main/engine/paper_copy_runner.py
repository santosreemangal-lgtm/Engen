"""Create deduplicated paper orders from verified monitored-wallet swap candidates.

This module never signs or broadcasts blockchain transactions. It only queues
paper orders. Execution is deliberately deferred until a trusted market-price
and liquidity provider is connected.
"""
import os
import threading
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

from engine.paper_simulator import PaperSimulator
from engine.market_data import fetch_market_price
from engine.transaction_analysis import WRAPPED_SOL_MINT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_SIMULATOR_DB = Path(
    os.environ.get(
        "PAPER_SIMULATOR_DB",
        str(PROJECT_ROOT / "data" / "paper_simulator.db"),
    )
)
_SIMULATOR = None
PAPER_SIMULATOR_LOCK = threading.RLock()


def get_paper_simulator():
    """Return the one persistent simulator shared by dashboard and monitor."""
    global _SIMULATOR
    with PAPER_SIMULATOR_LOCK:
        if _SIMULATOR is None:
            _SIMULATOR = PaperSimulator(db_path=PAPER_SIMULATOR_DB)
        return _SIMULATOR


def _human_amount(change):
    """Convert an integer base-unit delta to token units without float math."""
    raw = change.get("delta_amount")
    decimals = change.get("decimals")
    if (
        not isinstance(raw, str)
        or not (raw[1:].isdigit() if raw.startswith("-") else raw.isdigit())
        or not isinstance(decimals, int)
        or isinstance(decimals, bool)
        or not 0 <= decimals <= 30
    ):
        return None
    return Decimal(raw) / (Decimal(10) ** decimals)

def _aggregate_wallet_mint_changes(changes, wallet_address, mint, *, source):
    """Aggregate one wallet/mint's account deltas without mixing SOL representations."""
    matches = [
        item for item in changes
        if isinstance(item, dict)
        and item.get("owner") == wallet_address
        and item.get("mint") == mint
        and (
            (source == "native_sol" and item.get("source") == "native_sol")
            or (source == "spl" and item.get("source") != "native_sol")
        )
    ]
    if not matches:
        return None
    decimals = {item.get("decimals") for item in matches}
    if len(decimals) != 1:
        return None
    decimal_count = next(iter(decimals))
    if not isinstance(decimal_count, int) or isinstance(decimal_count, bool) or not 0 <= decimal_count <= 30:
        return None

    def raw_integer(value):
        if not isinstance(value, str):
            return None
        if value.startswith("-"):
            return int(value) if value[1:].isdigit() else None
        return int(value) if value.isdigit() else None

    deltas = [raw_integer(item.get("delta_amount")) for item in matches]
    if any(value is None for value in deltas):
        return None
    aggregate = {
        "owner": wallet_address,
        "mint": mint,
        "decimals": decimal_count,
        "delta_amount": str(sum(deltas)),
        "source": "native_sol" if source == "native_sol" else "spl",
    }
    for field in ("pre_amount", "post_amount"):
        values = [raw_integer(item.get(field)) for item in matches]
        aggregate[field] = str(sum(values)) if all(value is not None for value in values) else None
    return aggregate

def execute_pending_paper_orders(wallet_address=None, simulator=None, price_provider=None):
    """Retry queued automatic paper orders using fresh public market snapshots."""
    simulator = simulator or get_paper_simulator()
    price_provider = price_provider or fetch_market_price
    now = datetime.now(timezone.utc).isoformat()
    outcomes = []

    for order in list(simulator.orders.values()):
        details = order.get("details") or {}
        if (
            order.get("status") != "CREATED"
            or details.get("origin") != "AUTOMATIC_WALLET_COPY"
            or (wallet_address is not None and details.get("source_wallet") != wallet_address)
        ):
            continue
        if order.get("side") == "SELL":
            fraction_raw = details.get("source_sell_fraction")
            try:
                sell_fraction = Decimal(str(fraction_raw))
            except Exception:
                sell_fraction = Decimal("0")
            owned_quantity = Decimal(str(simulator.balances.get(order.get("asset_mint"), "0")))
            if not sell_fraction.is_finite() or sell_fraction <= 0 or sell_fraction > 1:
                outcomes.append({
                    "order_id": order.get("order_id"),
                    "signal_id": order.get("signal_id"),
                    "status": "WAITING_FOR_POSITION",
                    "reason": "Source sell fraction is unavailable or invalid; no paper sell was executed.",
                })
                continue
            if owned_quantity <= 0:
                outcomes.append({
                    "order_id": order.get("order_id"),
                    "signal_id": order.get("signal_id"),
                    "status": "WAITING_FOR_POSITION",
                    "reason": "No copied token position is currently held; sell remains queued.",
                })
                continue
            order["quantity"] = str(owned_quantity * sell_fraction)
            simulator._persist(order)
        try:
            market_price = price_provider(order.get("asset_mint"))
        except Exception:
            market_price = None
        if market_price is None:
            outcomes.append({
                "order_id": order.get("order_id"),
                "signal_id": order.get("signal_id"),
                "status": "WAITING_FOR_MARKET_DATA",
                "reason": "No usable public USD price/liquidity snapshot; order remains queued.",
            })
            continue
        try:
            result = simulator.execute_order(
                order["order_id"],
                market_price,
                now=now,
                max_price_age_seconds=60,
                slippage_bps=50,
                fee_bps=10,
                min_liquidity_usd=10000,
                max_liquidity_age_seconds=60,
                require_liquidity=True,
            )
        except Exception:
            outcomes.append({
                "order_id": order.get("order_id"),
                "signal_id": order.get("signal_id"),
                "status": "EXECUTION_ERROR",
                "reason": "Paper execution failed; no real transaction was sent.",
            })
            continue
        outcomes.append({
            "order_id": order.get("order_id"),
            "signal_id": order.get("signal_id"),
            "status": result.get("status", "UNKNOWN"),
            "reason": result.get("reason"),
        })
    return outcomes


def queue_paper_orders(wallet_address, results, simulator=None, allowed_sides="BOTH", copy_settings=None):
    """Queue conservative paper-copy orders; buys use virtual USD sizing and sells are position-capped."""
    if not isinstance(allowed_sides, str) or allowed_sides.upper() not in ("BUY", "SELL", "BOTH", "NONE"):
        raise ValueError("allowed_sides must be BUY, SELL, BOTH, or NONE")
    mode = allowed_sides.upper()
    allowed = {"BUY", "SELL"} if mode == "BOTH" else ({mode} if mode in ("BUY", "SELL") else set())
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A monitored public wallet address is required.")
    if not isinstance(results, list):
        raise ValueError("Monitor results must be a list.")
    simulator = simulator or get_paper_simulator()
    settings = copy_settings if isinstance(copy_settings, dict) else {}
    sizing_mode = settings.get("sizing_mode", "SOURCE_PERCENT")
    aliases = {"FIXED_PERCENTAGE": "SOURCE_PERCENT", "FIXED_AMOUNT": "FIXED_USD",
               "MIRROR_BUY_SELL_AMOUNT": "MIRROR_AMOUNT"}
    sizing_mode = aliases.get(sizing_mode, sizing_mode)
    if sizing_mode not in ("SOURCE_PERCENT", "FIXED_USD", "MIRROR_AMOUNT"):
        raise ValueError("sizing_mode must be SOURCE_PERCENT, FIXED_USD, or MIRROR_AMOUNT")
    try:
        sizing_value = Decimal(str(settings.get("sizing_value", 5 if sizing_mode == "SOURCE_PERCENT" else 10)))
        max_buy_usd = settings.get("max_buy_usd", "10")
        max_buy_usd = None if max_buy_usd in (None, "") else Decimal(str(max_buy_usd))
    except Exception as exc:
        raise ValueError("Sizing and maximum BUY values must be valid numbers.") from exc
    if not sizing_value.is_finite() or sizing_value <= 0:
        raise ValueError("sizing_value must be a positive finite number.")
    if sizing_mode == "SOURCE_PERCENT" and sizing_value >= 100:
        raise ValueError("Fixed percentage must be greater than 0 and less than 100.")
    if sizing_mode == "FIXED_USD" and sizing_value > Decimal("1000000"):
        raise ValueError("Fixed USD paper size exceeds the safety limit.")
    if max_buy_usd is not None and (
        not max_buy_usd.is_finite() or max_buy_usd <= 0 or max_buy_usd > Decimal("1000000")
    ):
        raise ValueError("Maximum BUY amount must be positive and at most $1,000,000.")
    queued = []

    for result in results:
        if not isinstance(result, dict):
            continue
        signature = result.get("signature")
        observation = result.get("signature_observation")
        signal = result.get("trade_signal")
        if (
            not isinstance(signature, str) or not signature.strip()
            or not isinstance(observation, dict)
            or observation.get("confirmation_status") not in ("confirmed", "finalized")
            or result.get("status") != "success"
            or result.get("metadata_complete") is not True
            or not isinstance(result.get("signers"), list)
            or wallet_address not in result.get("signers", [])
            or not isinstance(signal, dict)
            or signal.get("classification") not in ("BUY_CANDIDATE", "SELL_CANDIDATE")
        ):
            continue

        asset_mints = signal.get("asset_mints")
        quote_mints = signal.get("quote_mints")
        changes = result.get("token_balance_changes")
        if (
            not isinstance(asset_mints, list) or len(asset_mints) != 1
            or not isinstance(quote_mints, list) or len(quote_mints) != 1
            or not isinstance(changes, list)
        ):
            continue

        asset_mint, quote_mint = asset_mints[0], quote_mints[0]
        # A wallet can hold the same mint in multiple token accounts. Aggregate
        # those account-level deltas before deciding whether this is a copyable swap.
        # Never mix native SOL movement with wrapped-SOL SPL-token movement.
        asset_change = _aggregate_wallet_mint_changes(
            changes, wallet_address, asset_mint, source="spl"
        )
        if quote_mint == WRAPPED_SOL_MINT:
            native_quote = _aggregate_wallet_mint_changes(
                changes, wallet_address, quote_mint, source="native_sol"
            )
            quote_change = native_quote or _aggregate_wallet_mint_changes(
                changes, wallet_address, quote_mint, source="spl"
            )
        else:
            quote_change = _aggregate_wallet_mint_changes(
                changes, wallet_address, quote_mint, source="spl"
            )
        if asset_change is None or quote_change is None:
            continue
        asset_amount = _human_amount(asset_change)
        quote_amount = _human_amount(quote_change)
        if asset_amount is None or quote_amount is None:
            continue
        side = "BUY" if signal["classification"] == "BUY_CANDIDATE" else "SELL"
        if (side == "BUY" and (asset_amount <= 0 or quote_amount >= 0)) or (
            side == "SELL" and (asset_amount >= 0 or quote_amount <= 0)
        ):
            continue
        if side not in allowed:
            continue

        source_quantity = abs(asset_amount)
        target_notional_usd = None
        if side == "BUY":
            balances = getattr(simulator, "balances", {})
            virtual_usd = balances.get("USD") if isinstance(balances, dict) else None
            if virtual_usd is None:
                # A real PaperSimulator must have an explicit virtual USD balance.
                # Lightweight test doubles without balances may still validate queue logic.
                if hasattr(simulator, "balances"):
                    continue
            else:
                virtual_usd = Decimal(str(virtual_usd))
                if virtual_usd <= 0:
                    continue
            if sizing_mode == "SOURCE_PERCENT":
                if virtual_usd is None:
                    continue
                target_notional_usd = virtual_usd * sizing_value / Decimal("100")
            elif sizing_mode == "FIXED_USD":
                target_notional_usd = sizing_value
            else:
                # Only USDC/USDT notionals are safely interpretable as USD here.
                stable_quote_mints = {
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                }
                if quote_mint not in stable_quote_mints:
                    continue
                target_notional_usd = abs(quote_amount)
            if max_buy_usd is not None:
                target_notional_usd = min(target_notional_usd, max_buy_usd)
            if virtual_usd is not None:
                target_notional_usd = min(target_notional_usd, virtual_usd)
            if target_notional_usd <= 0:
                continue
            order_quantity = Decimal("1")  # simulator converts the USD target using a fresh price
        else:
            # Sell in proportion to the source wallet's observed position reduction,
            # capped at the entire remaining paper position. Never apply BUY sizing to SELL.
            pre_source_amount = asset_change.get("pre_amount")
            if pre_source_amount is None:
                continue
            source_before = _human_amount({
                "delta_amount": str(pre_source_amount),
                "decimals": asset_change.get("decimals"),
            })
            if source_before is None or not source_before.is_finite() or source_before <= 0 or source_quantity > source_before:
                continue
            sell_fraction = source_quantity / source_before
            if sell_fraction <= 0 or sell_fraction > 1:
                continue
            # Resolve quantity at execution time, after earlier queued BUYs have had
            # a chance to fill. This prevents same-scan BUY→SELL signals from being lost.
            order_quantity = Decimal("1")

        signal_id = f"wallet-copy:{wallet_address}:{signature}"
        created_at = datetime.now(timezone.utc).isoformat()
        order = simulator.create_order(
            signal_id=signal_id, side=side, asset_mint=asset_mint,
            quantity=str(order_quantity), quote_mint="USD", created_at=created_at,
            details={
                "sizing_mode": sizing_mode if side == "BUY" else "SOURCE_POSITION_REDUCTION",
                "sizing_value": str(sizing_value) if side == "BUY" else None,
                "target_notional_usd": str(target_notional_usd) if target_notional_usd is not None else None,
                "max_buy_usd": str(max_buy_usd) if max_buy_usd is not None else None,
                "source_asset_quantity": str(source_quantity),
                "source_asset_pre_amount": asset_change.get("pre_amount"),
                "source_asset_post_amount": asset_change.get("post_amount"),
                "source_sell_fraction": str(source_quantity / source_before) if side == "SELL" else None,
                "origin": "AUTOMATIC_WALLET_COPY",
                "source_quote_mint": quote_mint,
                "source_quote_amount": str(abs(quote_amount)),
                "source_wallet": wallet_address,
                "source_signature": signature,
                "source_slot": observation.get("slot"),
                "source_block_time": observation.get("block_time"),
                "source_classification": signal["classification"],
                "execution_state": "AWAITING_VERIFIED_MARKET_DATA",
                "real_trading": False,
                "transaction_broadcast": False,
            },
        )
        if order.get("status") == "DUPLICATE":
            continue
        queued.append({
            "signature": signature, "order_id": order.get("order_id"), "side": side,
            "asset_mint": asset_mint, "quantity": str(order_quantity),
            "sizing_mode": sizing_mode if side == "BUY" else "SOURCE_POSITION_REDUCTION",
            "sizing_value": str(sizing_value) if side == "BUY" else None,
            "target_notional_usd": str(target_notional_usd) if target_notional_usd is not None else None,
            "source_quote_mint": quote_mint, "status": order.get("status"),
            "execution_state": "AWAITING_VERIFIED_MARKET_DATA",
        })
    return queued
