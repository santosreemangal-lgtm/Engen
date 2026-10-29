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

def _aggregate_owner_mint_change(changes, wallet_address, mint, *, native_sol=False):
    """Aggregate account-level balance rows for one wallet/mint without mixing SOL sources."""
    selected = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        if item.get("owner") != wallet_address or item.get("mint") != mint:
            continue
        is_native = item.get("source") == "native_sol"
        if is_native != native_sol:
            continue
        raw_delta = item.get("delta_amount")
        decimals = item.get("decimals")
        if (
            not isinstance(raw_delta, str)
            or not (raw_delta[1:].isdigit() if raw_delta.startswith("-") else raw_delta.isdigit())
            or not isinstance(decimals, int)
            or isinstance(decimals, bool)
            or not 0 <= decimals <= 30
        ):
            return None
        selected.append(item)
    if not selected:
        return None

    decimals = selected[0]["decimals"]
    if any(item["decimals"] != decimals for item in selected):
        return None

    total_delta = sum(int(item["delta_amount"]) for item in selected)
    aggregated = {
        "owner": wallet_address,
        "mint": mint,
        "decimals": decimals,
        "delta_amount": str(total_delta),
        "source": "native_sol" if native_sol else "token",
    }
    for field in ("pre_amount", "post_amount"):
        raw_values = [item.get(field) for item in selected]
        if all(
            isinstance(value, str)
            and value.isdigit()
            for value in raw_values
        ):
            aggregated[field] = str(sum(int(value) for value in raw_values))
    return aggregated


def _quote_change_matches_side(amount, side):
    if amount is None or not amount.is_finite() or amount == 0:
        return False
    return amount < 0 if side == "BUY" else amount > 0


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
        if order.get("side") == "BUY":
            # Keep orders queued when the user has not funded the virtual ledger
            # yet. Do not reject or silently discard a detected paper-copy signal.
            balances = getattr(simulator, "balances", {})
            raw_cash = balances.get(order.get("quote_mint", "USD"), "0") if isinstance(balances, dict) else "0"
            try:
                virtual_cash = Decimal(str(raw_cash))
            except Exception:
                virtual_cash = Decimal("0")
            if not virtual_cash.is_finite() or virtual_cash <= 0:
                outcomes.append({
                    "order_id": order.get("order_id"),
                    "signal_id": order.get("signal_id"),
                    "status": "WAITING_FOR_VIRTUAL_BALANCE",
                    "reason": "Set a positive virtual USD balance before this paper BUY can execute.",
                })
                continue

            target_raw = details.get("target_notional_usd")
            try:
                target = Decimal(str(target_raw)) if target_raw is not None else None
                sizing_value = Decimal(str(details.get("sizing_value", "0")))
                max_buy = Decimal(str(details.get("max_buy_usd"))) if details.get("max_buy_usd") not in (None, "") else None
            except Exception:
                target, sizing_value, max_buy = None, Decimal("0"), None
            if target is None:
                sizing_mode = details.get("sizing_mode", "SOURCE_PERCENT")
                if sizing_mode == "SOURCE_PERCENT" and sizing_value > 0:
                    target = virtual_cash * sizing_value / Decimal("100")
                elif sizing_mode == "FIXED_USD" and sizing_value > 0:
                    target = sizing_value
                elif sizing_mode == "MIRROR_AMOUNT":
                    stable_quote_mints = {
                        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                    }
                    try:
                        source_quote = Decimal(str(details.get("source_quote_amount", "0")))
                    except Exception:
                        source_quote = Decimal("0")
                    source_quote_mint = details.get("source_quote_mint")
                    if source_quote_mint in stable_quote_mints and source_quote > 0:
                        target = source_quote
                    elif source_quote_mint and source_quote > 0:
                        # Non-stable quote assets (most commonly SOL) must be
                        # converted through a fresh public USD price, never guessed.
                        try:
                            quote_price = price_provider(source_quote_mint)
                            price_value = Decimal(str(quote_price.price)) if quote_price is not None else Decimal("0")
                            if (
                                quote_price is not None
                                and quote_price.mint == source_quote_mint
                                and str(quote_price.currency).upper() == "USD"
                                and price_value.is_finite() and price_value > 0
                                and quote_price.is_fresh(max_age_seconds=60, now=now)
                            ):
                                target = source_quote * price_value
                        except Exception:
                            target = None
            if target is None or not target.is_finite() or target <= 0:
                waiting_for_price = (
                    details.get("sizing_mode") == "MIRROR_AMOUNT"
                    and details.get("source_quote_mint") not in {
                        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                    }
                )
                if waiting_for_price:
                    details["execution_state"] = "AWAITING_VERIFIED_MARKET_DATA"
                    order["details"] = details
                    simulator._persist(order)
                outcomes.append({
                    "order_id": order.get("order_id"),
                    "signal_id": order.get("signal_id"),
                    "status": "WAITING_FOR_MARKET_DATA" if waiting_for_price else "WAITING_FOR_VIRTUAL_BALANCE",
                    "reason": (
                        "A fresh public SOL/USD price is required to size this mirrored paper BUY."
                        if waiting_for_price else
                        "Paper BUY sizing is unavailable; check the saved copy settings and virtual balance."
                    ),
                })
                continue
            if max_buy is not None and max_buy.is_finite() and max_buy > 0:
                target = min(target, max_buy)
            target = min(target, virtual_cash)
            if target <= 0:
                outcomes.append({
                    "order_id": order.get("order_id"),
                    "signal_id": order.get("signal_id"),
                    "status": "WAITING_FOR_VIRTUAL_BALANCE",
                    "reason": "The virtual USD balance is too small for this paper BUY.",
                })
                continue
            details["target_notional_usd"] = str(target)
            details["execution_state"] = "AWAITING_VERIFIED_MARKET_DATA"
            order["details"] = details
            simulator._persist(order)

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
        side = "BUY" if signal["classification"] == "BUY_CANDIDATE" else "SELL"
        asset_change = _aggregate_owner_mint_change(
            changes, wallet_address, asset_mint, native_sol=False
        )
        asset_amount = _human_amount(asset_change) if asset_change is not None else None
        if asset_amount is None:
            continue

        # Token and native-SOL rows use the same wrapped-SOL mint label in the
        # analyzer, but represent separate balance sources. Prefer the SPL quote
        # delta only when it agrees with the verified swap direction; otherwise
        # fall back to the fee-adjusted native-SOL delta. Never double-count them.
        quote_change = _aggregate_owner_mint_change(
            changes, wallet_address, quote_mint, native_sol=False
        )
        quote_amount = _human_amount(quote_change) if quote_change is not None else None
        if (
            quote_mint == WRAPPED_SOL_MINT
            and not _quote_change_matches_side(quote_amount, side)
        ):
            native_quote_change = _aggregate_owner_mint_change(
                changes, wallet_address, quote_mint, native_sol=True
            )
            native_quote_amount = (
                _human_amount(native_quote_change)
                if native_quote_change is not None else None
            )
            if _quote_change_matches_side(native_quote_amount, side):
                quote_change, quote_amount = native_quote_change, native_quote_amount
        if not _quote_change_matches_side(quote_amount, side):
            continue
        if (side == "BUY" and asset_amount <= 0) or (
            side == "SELL" and asset_amount >= 0
        ):
            continue
        if side not in allowed:
            continue

        source_quantity = abs(asset_amount)
        target_notional_usd = None
        if side == "BUY":
            balances = getattr(simulator, "balances", {})
            virtual_usd = balances.get("USD") if isinstance(balances, dict) else None
            if virtual_usd is not None:
                try:
                    virtual_usd = Decimal(str(virtual_usd))
                except Exception:
                    virtual_usd = None
                if virtual_usd is not None and (not virtual_usd.is_finite() or virtual_usd <= 0):
                    virtual_usd = None
            if sizing_mode == "SOURCE_PERCENT":
                # If no virtual cash has been set yet, queue the signal and
                # resolve its USD size after the user funds the paper ledger.
                target_notional_usd = (
                    virtual_usd * sizing_value / Decimal("100")
                    if virtual_usd is not None else None
                )
            elif sizing_mode == "FIXED_USD":
                target_notional_usd = sizing_value
            else:
                # Stablecoin notionals are already USD. For SOL-quoted swaps,
                # defer conversion until execution can verify a fresh SOL/USD price.
                stable_quote_mints = {
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                }
                if quote_mint in stable_quote_mints:
                    target_notional_usd = abs(quote_amount)
                else:
                    # Queue the verified signal even for a non-stable quote.
                    # USD sizing is deferred until a fresh quote/USD price exists.
                    target_notional_usd = None
            if target_notional_usd is not None:
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
                "execution_state": (
                    "AWAITING_VIRTUAL_BALANCE"
                    if side == "BUY" and (virtual_usd is None or virtual_usd <= 0)
                    else "AWAITING_VERIFIED_MARKET_DATA"
                ),
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
            "execution_state": (order.get("details") or {}).get(
                "execution_state", "AWAITING_VERIFIED_MARKET_DATA"
            ),
        })
    return queued
