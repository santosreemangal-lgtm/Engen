"""Offline-only paper trading ledger; never submits real transactions.

Prices and quantities must be supplied by the caller. This module does not
fetch market prices, sign transactions, or connect to a wallet.
"""

from datetime import datetime, timezone
from threading import RLock


class PaperTradingError(ValueError):
    """Raised when a simulated trade violates ledger rules."""


class PaperTrader:
    """A small in-memory virtual portfolio for deterministic offline tests."""

    def __init__(self, starting_balance=1000.0, max_position_fraction=0.25):
        self._validate_positive(starting_balance, "starting_balance")
        if (
            isinstance(max_position_fraction, bool)
            or not isinstance(max_position_fraction, (int, float))
            or not 0 < max_position_fraction <= 1
        ):
            raise PaperTradingError("max_position_fraction must be in (0, 1].")
        self._initial_balance = float(starting_balance)
        self._cash = float(starting_balance)
        self._max_position_fraction = float(max_position_fraction)
        self._positions = {}
        self._history = []
        self._paused = False
        self._lock = RLock()

    @staticmethod
    def _validate_positive(value, name):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise PaperTradingError("%s must be a positive number." % name)

    @staticmethod
    def _validate_token(token):
        if not isinstance(token, str) or not token.strip():
            raise PaperTradingError("A token identifier is required.")
        return token.strip()

    def pause(self):
        with self._lock:
            self._paused = True
            return self.status()

    def resume(self):
        with self._lock:
            self._paused = False
            return self.status()

    def status(self):
        with self._lock:
            return {
                "mode": "paper",
                "paused": self._paused,
                "cash": round(self._cash, 8),
                "starting_balance": round(self._initial_balance, 8),
                "positions": {
                    token: {
                        "quantity": round(position["quantity"], 8),
                        "average_entry_price": round(position["average_entry_price"], 8),
                    }
                    for token, position in self._positions.items()
                },
                "history_count": len(self._history),
                "live_trading_enabled": False,
            }

    def buy(self, token, quantity, price):
        token = self._validate_token(token)
        self._validate_positive(quantity, "quantity")
        self._validate_positive(price, "price")
        quantity, price = float(quantity), float(price)
        cost = quantity * price

        with self._lock:
            if self._paused:
                raise PaperTradingError("Paper trading is paused.")
            if cost > self._cash:
                raise PaperTradingError("Insufficient virtual cash.")
            max_cost = self._initial_balance * self._max_position_fraction
            existing_cost = 0.0
            position = self._positions.get(token)
            if position:
                existing_cost = position["quantity"] * position["average_entry_price"]
            if existing_cost + cost > max_cost + 1e-9:
                raise PaperTradingError("Position size exceeds the configured risk limit.")

            old_quantity = position["quantity"] if position else 0.0
            new_quantity = old_quantity + quantity
            old_cost = old_quantity * position["average_entry_price"] if position else 0.0
            self._positions[token] = {
                "quantity": new_quantity,
                "average_entry_price": (old_cost + cost) / new_quantity,
            }
            self._cash -= cost
            return self._record("buy", token, quantity, price, cost, 0.0)

    def sell(self, token, quantity, price):
        token = self._validate_token(token)
        self._validate_positive(quantity, "quantity")
        self._validate_positive(price, "price")
        quantity, price = float(quantity), float(price)

        with self._lock:
            if self._paused:
                raise PaperTradingError("Paper trading is paused.")
            position = self._positions.get(token)
            if not position or quantity > position["quantity"] + 1e-9:
                raise PaperTradingError("Insufficient virtual token quantity.")
            proceeds = quantity * price
            pnl = (price - position["average_entry_price"]) * quantity
            position["quantity"] -= quantity
            if position["quantity"] <= 1e-9:
                del self._positions[token]
            self._cash += proceeds
            return self._record("sell", token, quantity, price, proceeds, pnl)

    def _record(self, side, token, quantity, price, cash_delta, realized_pnl):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "side": side,
            "token": token,
            "quantity": round(quantity, 8),
            "price": round(price, 8),
            "cash_delta": round(cash_delta if side == "sell" else -cash_delta, 8),
            "realized_pnl": round(realized_pnl, 8),
            "mode": "paper",
        }
        self._history.append(entry)
        return dict(entry)

    def history(self, limit=100):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise PaperTradingError("history limit must be a positive integer.")
        with self._lock:
            return [dict(item) for item in self._history[-limit:]]
