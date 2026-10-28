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
from engine.transaction_analysis import QUOTE_TOKEN_MINTS
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


def queue_paper_orders(wallet_address, results, simulator=None, allowed_sides="BOTH"):
    """Queue confirmed swap candidates allowed by BUY, SELL, or BOTH copy mode."""
    if not isinstance(allowed_sides, str) or allowed_sides.upper() not in ("BUY", "SELL", "BOTH", "NONE"):
        raise ValueError("allowed_sides must be BUY, SELL, BOTH, or NONE")
    mode = allowed_sides.upper()
    allowed = {"BUY", "SELL"} if mode == "BOTH" else ({mode} if mode in ("BUY", "SELL") else set())
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("A monitored public wallet address is required.")
    if not isinstance(results, list):
        raise ValueError("Monitor results must be a list.")
    simulator = simulator or get_paper_simulator()
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
            or quote_mints[0] not in QUOTE_TOKEN_MINTS
            or not isinstance(changes, list)
        ):
            continue

        asset_mint, quote_mint = asset_mints[0], quote_mints[0]
        asset_changes = [
            item for item in changes
            if isinstance(item, dict)
            and item.get("owner") == wallet_address
            and item.get("mint") == asset_mint
            and item.get("source") != "native_sol"
        ]
        quote_changes = [
            item for item in changes
            if isinstance(item, dict)
            and item.get("owner") == wallet_address
            and (
                item.get("mint") == quote_mint
                or (
                    quote_mint == WRAPPED_SOL_MINT
                    and item.get("source") == "native_sol"
                    and item.get("mint") == WRAPPED_SOL_MINT
                )
            )
        ]
        if len(asset_changes) != 1 or len(quote_changes) != 1:
            continue

        asset_amount = _human_amount(asset_changes[0])
        quote_amount = _human_amount(quote_changes[0])
        if asset_amount is None or quote_amount is None:
            continue
        side = "BUY" if signal["classification"] == "BUY_CANDIDATE" else "SELL"
        if (
            side == "BUY" and (asset_amount <= 0 or quote_amount >= 0)
        ) or (
            side == "SELL" and (asset_amount >= 0 or quote_amount <= 0)
        ):
            continue

        if side not in allowed:
            continue

        signal_id = f"wallet-copy:{wallet_address}:{signature}"
        created_at = datetime.now(timezone.utc).isoformat()
        order = simulator.create_order(
            signal_id=signal_id,
            side=side,
            asset_mint=asset_mint,
            quantity=str(abs(asset_amount)),
            quote_mint="USD",
            created_at=created_at,
            details={
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
            "signature": signature,
            "order_id": order.get("order_id"),
            "side": side,
            "asset_mint": asset_mint,
            "quantity": str(abs(asset_amount)),
            "source_quote_mint": quote_mint,
            "status": order.get("status"),
            "execution_state": "AWAITING_VERIFIED_MARKET_DATA",
        })

    return queued
