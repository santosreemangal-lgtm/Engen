"""Conservative average-cost P&L accounting for simulation and reporting.

All monetary values use Decimal. Prices must be sourced and timestamped. Unknown
basis or stale/missing prices produce unavailable P&L, never a fabricated zero.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


PORTFOLIO_CURRENCY = "USD"
COST_BASIS_METHOD = "average_cost"


def decimal_value(value, field, *, allow_zero=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a valid decimal.") from exc
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise ValueError(f"{field} must be finite and {'non-negative' if allow_zero else 'positive'}.")
    return number


def roi_percentage(pnl, cost_basis):
    """Return percentage ROI against a positive, exact cost-basis denominator."""
    basis = Decimal(str(cost_basis))
    if not basis.is_finite() or basis <= 0:
        return None
    return str(Decimal(str(pnl)) * Decimal("100") / basis)


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be a timezone-aware ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Timestamp must be ISO-8601.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class MarketPrice:
    mint: str
    price: Decimal
    currency: str
    source: str
    observed_at: str
    liquidity_usd: Decimal = None
    liquidity_source: str = None
    liquidity_observed_at: str = None

    def __post_init__(self):
        if not isinstance(self.mint, str) or not self.mint.strip():
            raise ValueError("Price needs a token mint.")
        object.__setattr__(self, "price", decimal_value(self.price, "Price"))
        if self.currency != PORTFOLIO_CURRENCY:
            raise ValueError("Prices must be converted to USD by a documented source before valuation.")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Price source is required.")
        parse_timestamp(self.observed_at)
        if self.liquidity_usd is not None:
            object.__setattr__(
                self, "liquidity_usd",
                decimal_value(self.liquidity_usd, "USD liquidity", allow_zero=True),
            )
            if not isinstance(self.liquidity_source, str) or not self.liquidity_source.strip():
                raise ValueError("Liquidity source is required when liquidity is supplied.")
            parse_timestamp(self.liquidity_observed_at)
        elif self.liquidity_source is not None or self.liquidity_observed_at is not None:
            raise ValueError("Liquidity source/time cannot be supplied without a liquidity value.")

    def liquidity_age_seconds(self, now=None):
        if self.liquidity_usd is None:
            return None
        current = now or datetime.now(timezone.utc)
        if isinstance(current, str):
            current = parse_timestamp(current)
        return max(0, int((current - parse_timestamp(self.liquidity_observed_at)).total_seconds()))

    def age_seconds(self, now=None):
        current = now or datetime.now(timezone.utc)
        if isinstance(current, str):
            current = parse_timestamp(current)
        return max(0, int((current - parse_timestamp(self.observed_at)).total_seconds()))

    def is_fresh(self, *, max_age_seconds=60, now=None):
        if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) or max_age_seconds < 0:
            raise ValueError("max_age_seconds must be a non-negative integer.")
        current = now or datetime.now(timezone.utc)
        if isinstance(current, str):
            current = parse_timestamp(current)
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("now must be a timezone-aware datetime or ISO-8601 string.")
        age = (current.astimezone(timezone.utc) - parse_timestamp(self.observed_at)).total_seconds()
        # A future-dated provider snapshot is not a fresh price.
        return 0 <= age <= max_age_seconds


def portfolio_valuation_usd(balances, market_prices, *, max_age_seconds=60, now=None):
    """Value virtual USD cash and token balances using fresh, mint-matched USD prices.

    Cost basis is deliberately irrelevant to market value. Missing, stale, future,
    or mismatched prices keep that holding visible but withhold the aggregate total.
    """
    if not isinstance(balances, dict) or not isinstance(market_prices, dict):
        raise ValueError("Balances and market prices must be dictionaries.")
    if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be a non-negative integer.")

    cash = decimal_value(balances.get("USD", "0"), "Cash balance", allow_zero=True)
    holdings = []
    token_total = Decimal("0")
    all_holdings_priced = True
    valuation_notes = []

    for mint in sorted(balances):
        if not isinstance(mint, str) or not mint.strip():
            raise ValueError("Every portfolio balance must have a token mint.")
        if mint == "USD":
            continue
        quantity = decimal_value(balances[mint], "Token balance", allow_zero=True)
        if quantity == 0:
            continue

        price = market_prices.get(mint)
        status = "calculated"
        price_age = None
        market_value = None
        if not isinstance(price, MarketPrice) or price.mint != mint:
            status = "price_unavailable" if price is None else "invalid_or_mismatched_price"
        else:
            current = now or datetime.now(timezone.utc)
            if isinstance(current, str):
                current = parse_timestamp(current)
            if not isinstance(current, datetime) or current.tzinfo is None:
                raise ValueError("now must be a timezone-aware datetime or ISO-8601 string.")
            age = (current.astimezone(timezone.utc) - parse_timestamp(price.observed_at)).total_seconds()
            price_age = max(0, int(age))
            if age < 0:
                status = "future_price"
            elif age > max_age_seconds:
                status = "stale_price"
            else:
                market_value = quantity * price.price
                token_total += market_value

        holdings.append({
            "mint": mint,
            "quantity": format(quantity, "f"),
            "currency": PORTFOLIO_CURRENCY,
            "price_usd": format(price.price, "f") if market_value is not None else None,
            "market_value_usd": format(market_value, "f") if market_value is not None else None,
            "status": status,
            "price_source": price.source if isinstance(price, MarketPrice) else None,
            "price_timestamp": price.observed_at if isinstance(price, MarketPrice) else None,
            "price_age_seconds": price_age,
        })
        if market_value is None:
            all_holdings_priced = False
            valuation_notes.append(status + ":" + mint)

    token_value = format(token_total, "f") if all_holdings_priced else None
    portfolio_value = format(cash + token_total, "f") if all_holdings_priced else None
    return {
        "currency": PORTFOLIO_CURRENCY,
        "cash_balance_usd": format(cash, "f"),
        "token_holdings_value_usd": token_value,
        "token_holdings": holdings,
        "portfolio_value_usd": portfolio_value,
        "total_portfolio_value_usd": portfolio_value,
        "portfolio_value_status": "calculated" if all_holdings_priced else "incomplete_market_valuation",
        "portfolio_value_note": (
            "Cash and all token holdings are valued in USD using fresh, mint-matched prices."
            if all_holdings_priced else
            "Total value is withheld until every token holding has a fresh, mint-matched USD price."
        ),
        "portfolio_valuation_notes": valuation_notes,
    }


class AverageCostLedger:
    """Track positions and cash flows; unknown basis remains unknown."""

    def __init__(self, currency=PORTFOLIO_CURRENCY):
        if currency != PORTFOLIO_CURRENCY:
            raise ValueError("Only USD portfolio valuation is currently supported.")
        self.currency = currency
        self.positions = {}
        self.events = []

    def _position(self, mint):
        if not isinstance(mint, str) or not mint.strip():
            raise ValueError("Token mint is required.")
        return self.positions.setdefault(mint, {
            "quantity": Decimal("0"),
            "cost_basis": Decimal("0"),
            "basis_complete": True,
        })

    def buy(self, mint, quantity, unit_price, *, fee=0, fee_included=False,
            network_fee=0, timestamp=None):
        qty = decimal_value(quantity, "Quantity")
        price = decimal_value(unit_price, "Unit price")
        fee_value = decimal_value(fee, "Fee", allow_zero=True)
        network = decimal_value(network_fee, "Network fee", allow_zero=True)
        if not isinstance(fee_included, bool):
            raise ValueError("fee_included must be boolean.")
        total_cost = qty * price
        if not fee_included:
            total_cost += fee_value
        total_cost += network
        pos = self._position(mint)
        if pos["quantity"] > 0 and not pos["basis_complete"]:
            # Once unknown-cost units exist, average cost for the combined
            # fungible position cannot be reconstructed safely.
            pos["quantity"] += qty
        else:
            pos["quantity"] += qty
            pos["cost_basis"] += total_cost
        self.events.append({"type": "BUY", "mint": mint, "quantity": str(qty),
                            "cash_cost": str(total_cost), "currency": self.currency,
                            "timestamp": timestamp, "realized_pnl": "0"})
        return dict(self.events[-1])

    def sell(self, mint, quantity, unit_price, *, fee=0, fee_included=False,
             network_fee=0, timestamp=None):
        qty = decimal_value(quantity, "Quantity")
        price = decimal_value(unit_price, "Unit price")
        fee_value = decimal_value(fee, "Fee", allow_zero=True)
        network = decimal_value(network_fee, "Network fee", allow_zero=True)
        if not isinstance(fee_included, bool):
            raise ValueError("fee_included must be boolean.")
        pos = self._position(mint)
        if qty > pos["quantity"]:
            raise ValueError("Cannot sell more than the tracked position.")
        proceeds = qty * price
        if not fee_included:
            proceeds -= fee_value
        proceeds -= network
        if proceeds < 0:
            raise ValueError("Fees cannot exceed sale proceeds.")
        if pos["quantity"] == 0:
            raise ValueError("Cannot sell an empty position.")
        if pos["basis_complete"]:
            allocated_cost = pos["cost_basis"] * qty / pos["quantity"]
            realized = proceeds - allocated_cost
            pos["cost_basis"] -= allocated_cost
        else:
            allocated_cost = None
            realized = None
        pos["quantity"] -= qty
        if pos["quantity"] == 0:
            pos["cost_basis"] = Decimal("0")
            pos["basis_complete"] = True
        event = {"type": "SELL", "mint": mint, "quantity": str(qty),
                 "cash_proceeds": str(proceeds), "currency": self.currency,
                 "allocated_cost_basis": None if allocated_cost is None else str(allocated_cost),
                 "realized_pnl": None if realized is None else str(realized),
                 "pnl_status": "unavailable_unknown_cost_basis" if realized is None else "calculated",
                 "timestamp": timestamp}
        self.events.append(event)
        return dict(event)

    def deposit(self, mint, quantity, *, unit_price=None, timestamp=None):
        qty = decimal_value(quantity, "Quantity")
        pos = self._position(mint)
        if unit_price is None:
            pos["basis_complete"] = False
        else:
            price = decimal_value(unit_price, "Unit price")
            if pos["quantity"] and not pos["basis_complete"]:
                pos["basis_complete"] = False
            else:
                pos["cost_basis"] += qty * price
        pos["quantity"] += qty
        event = {"type": "DEPOSIT", "mint": mint, "quantity": str(qty),
                 "trading_pnl": "0", "basis_status": "known" if unit_price is not None else "unknown",
                 "timestamp": timestamp}
        self.events.append(event)
        return dict(event)

    def airdrop(self, mint, quantity, *, timestamp=None):
        event = self.deposit(mint, quantity, unit_price=None, timestamp=timestamp)
        event["type"] = "AIRDROP"
        self.events[-1]["type"] = "AIRDROP"
        return event

    def withdrawal(self, mint, quantity, *, timestamp=None):
        qty = decimal_value(quantity, "Quantity")
        pos = self._position(mint)
        if qty > pos["quantity"]:
            raise ValueError("Cannot withdraw more than the tracked position.")
        # A withdrawal is not a trading sale and realizes no P&L.
        if pos["quantity"] and pos["basis_complete"]:
            allocated = pos["cost_basis"] * qty / pos["quantity"]
            pos["cost_basis"] -= allocated
        pos["quantity"] -= qty
        if pos["quantity"] == 0:
            pos["cost_basis"] = Decimal("0")
            pos["basis_complete"] = True
        event = {"type": "WITHDRAWAL", "mint": mint, "quantity": str(qty), "trading_pnl": "0",
                 "timestamp": timestamp}
        self.events.append(event)
        return dict(event)

    def internal_transfer(self, mint, quantity, *, from_wallet, to_wallet, timestamp=None):
        qty = decimal_value(quantity, "Quantity")
        if not from_wallet or not to_wallet or from_wallet == to_wallet:
            raise ValueError("Internal transfer needs distinct source and destination wallets.")
        if qty > self._position(mint)["quantity"]:
            raise ValueError("Cannot transfer more than the tracked position.")
        event = {"type": "INTERNAL_TRANSFER", "mint": mint, "quantity": str(qty),
                 "from_wallet": from_wallet, "to_wallet": to_wallet, "trading_pnl": "0",
                 "timestamp": timestamp}
        self.events.append(event)
        return dict(event)

    def unrealized_pnl(self, mint, market_price, *, max_age_seconds=60, now=None):
        pos = self._position(mint)
        if not isinstance(market_price, MarketPrice) or market_price.mint != mint:
            return {"pnl": None, "roi_pct": None, "status": "price_unavailable",
                    "currency": self.currency}
        age = market_price.age_seconds(now)
        if age > max_age_seconds:
            return {"pnl": None, "roi_pct": None, "status": "stale_price",
                    "currency": self.currency, "price_age_seconds": age,
                    "price_source": market_price.source}
        value = pos["quantity"] * market_price.price
        if not pos["basis_complete"]:
            return {"pnl": None, "roi_pct": None, "market_value": str(value),
                    "cost_basis": None, "status": "unknown_cost_basis",
                    "currency": self.currency, "price_age_seconds": age,
                    "price_source": market_price.source,
                    "price_timestamp": market_price.observed_at}
        pnl = value - pos["cost_basis"]
        return {"pnl": str(pnl), "market_value": str(value), "cost_basis": str(pos["cost_basis"]),
                "roi_pct": roi_percentage(pnl, pos["cost_basis"]),
                "currency": self.currency, "status": "calculated",
                "price_age_seconds": age, "price_source": market_price.source,
                "price_timestamp": market_price.observed_at}

    def snapshot(self, mint):
        pos = self._position(mint)
        return {"mint": mint, "quantity": str(pos["quantity"]),
                "cost_basis": str(pos["cost_basis"]) if pos["basis_complete"] else None,
                "basis_complete": pos["basis_complete"], "currency": self.currency,
                "cost_basis_method": COST_BASIS_METHOD}



def realized_pnl_from_fills(fills):
    """Rebuild realized average-cost P&L from a chronological fill sequence."""
    ledger = AverageCostLedger()
    by_mint = {}
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        side = fill.get("side")
        mint = fill.get("asset_mint")
        quantity = fill.get("quantity")
        price = fill.get("execution_price")
        fee = fill.get("fee", "0")
        network = fill.get("network_cost", "0")
        timestamp = fill.get("created_at")
        if side == "BUY":
            ledger.buy(mint, quantity, price, fee=fee, network_fee=network,
                       timestamp=timestamp)
        elif side == "SELL":
            tracked = Decimal(ledger.snapshot(mint)["quantity"])
            sold = decimal_value(quantity, "Quantity")
            if sold > tracked:
                ledger.deposit(mint, sold - tracked, unit_price=None, timestamp=timestamp)
            event = ledger.sell(mint, quantity, price, fee=fee, network_fee=network,
                                timestamp=timestamp)
            item = by_mint.setdefault(mint, {
                "pnl": Decimal("0"), "cost_basis_sold": Decimal("0"), "complete": True
            })
            if event["realized_pnl"] is None or event["allocated_cost_basis"] is None:
                item["complete"] = False
            else:
                item["pnl"] += Decimal(event["realized_pnl"])
                item["cost_basis_sold"] += Decimal(event["allocated_cost_basis"])
        else:
            raise ValueError("Fill side must be BUY or SELL.")
    return {
        mint: {
            "pnl": str(item["pnl"]) if item["complete"] else None,
            "cost_basis_sold": str(item["cost_basis_sold"]) if item["complete"] else None,
            "roi_pct": roi_percentage(item["pnl"], item["cost_basis_sold"])
                if item["complete"] else None,
            "status": "calculated" if item["complete"] else "unknown_cost_basis",
            "currency": PORTFOLIO_CURRENCY,
        }
        for mint, item in sorted(by_mint.items())
    }
