"""Paper trading with optional SQLite persistence. Never submits real trades."""

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from decimal import Decimal
from pathlib import Path


class PaperTrader:
    """Simulate swaps and optionally persist the paper ledger in SQLite."""

    def __init__(self, initial_balances=None, db_path=None):
        if initial_balances is None:
            initial_balances = {}
        if not isinstance(initial_balances, Mapping):
            raise ValueError("Initial balances must be a mapping.")

        self.balances = {}
        for mint, amount in initial_balances.items():
            if not isinstance(mint, str) or not mint:
                raise ValueError("Each balance needs a valid token mint.")
            try:
                quantity = Decimal(str(amount))
            except Exception as exc:
                raise ValueError("Invalid initial balance.") from exc
            if not quantity.is_finite() or quantity < 0:
                raise ValueError("Initial balances must be finite and non-negative.")
            self.balances[mint] = quantity

        self.processed_signatures = set()
        self.trades = []
        self.db_path = Path(db_path) if db_path is not None else None

        if self.db_path is not None:
            self._initialize_storage()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _initialize_storage(self):
        """Create the ledger and restore previously saved state, if any."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self._connect()) as connection:
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS paper_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS paper_balances (
                        mint TEXT PRIMARY KEY,
                        amount TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS paper_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signature TEXT NOT NULL UNIQUE,
                        side TEXT NOT NULL,
                        asset_mint TEXT NOT NULL,
                        asset_amount TEXT NOT NULL,
                        quote_mint TEXT NOT NULL,
                        quote_amount TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                initialized = connection.execute(
                    "SELECT value FROM paper_meta WHERE key = 'initialized'"
                ).fetchone()

                if initialized is None:
                    # Seed the account only once. Restarts must not reset it.
                    connection.executemany(
                        "INSERT INTO paper_balances (mint, amount) VALUES (?, ?)",
                        [
                            (mint, str(amount))
                            for mint, amount in self.balances.items()
                        ],
                    )
                    connection.execute(
                        "INSERT INTO paper_meta (key, value) VALUES ('initialized', '1')"
                    )
                else:
                    rows = connection.execute(
                        "SELECT mint, amount FROM paper_balances"
                    ).fetchall()
                    self.balances = {
                        mint: Decimal(amount) for mint, amount in rows
                    }

                rows = connection.execute("""
                    SELECT signature, side, asset_mint, asset_amount,
                           quote_mint, quote_amount, status
                    FROM paper_trades
                    ORDER BY id
                """).fetchall()

                self.trades = [
                    {
                        "signature": row[0],
                        "side": row[1],
                        "asset_mint": row[2],
                        "asset_amount": row[3],
                        "quote_mint": row[4],
                        "quote_amount": row[5],
                        "status": row[6],
                    }
                    for row in rows
                ]
                self.processed_signatures = {
                    trade["signature"] for trade in self.trades
                }

    def _persist_trade(self, trade, balances):
        """Save the balance changes and trade together in one transaction."""
        if self.db_path is None:
            return

        with closing(self._connect()) as connection:
            with connection:
                connection.executemany("""
                    INSERT INTO paper_balances (mint, amount)
                    VALUES (?, ?)
                    ON CONFLICT(mint) DO UPDATE SET amount = excluded.amount
                """, [
                    (mint, str(amount))
                    for mint, amount in balances.items()
                ])

                connection.execute("""
                    INSERT INTO paper_trades (
                        signature, side, asset_mint, asset_amount,
                        quote_mint, quote_amount, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade["signature"],
                    trade["side"],
                    trade["asset_mint"],
                    trade["asset_amount"],
                    trade["quote_mint"],
                    trade["quote_amount"],
                    trade["status"],
                ))

    @staticmethod
    def _delta_for_mint(changes, mint):
        selected = [
            item for item in changes
            if isinstance(item, Mapping) and item.get("mint") == mint
        ]
        if not selected:
            return None

        decimals = None
        raw_total = 0

        for item in selected:
            raw = item.get("delta_amount")
            places = item.get("decimals")

            if (
                not isinstance(raw, str)
                or not raw
                or raw in ("-", "+")
                or not raw.lstrip("-").isdigit()
                or not isinstance(places, int)
                or isinstance(places, bool)
                or places < 0
            ):
                return None

            if decimals is None:
                decimals = places
            elif decimals != places:
                return None

            raw_total += int(raw)

        return Decimal(raw_total) / (Decimal(10) ** decimals)

    def process(self, result):
        """Process one successful candidate without submitting a real trade."""
        if not isinstance(result, Mapping):
            return {"status": "SKIPPED", "reason": "Invalid transaction result."}

        signature = result.get("signature")
        if not isinstance(signature, str) or not signature.strip():
            return {"status": "SKIPPED", "reason": "Missing transaction signature."}

        if signature in self.processed_signatures:
            return {"status": "DUPLICATE", "signature": signature}

        if result.get("available") is not True or result.get("status") != "success":
            return {
                "status": "SKIPPED",
                "reason": "Transaction is unavailable or unsuccessful.",
            }

        signal = result.get("trade_signal")
        if not isinstance(signal, Mapping):
            return {"status": "SKIPPED", "reason": "No trade signal."}

        action = signal.get("classification")
        if action not in ("BUY_CANDIDATE", "SELL_CANDIDATE"):
            return {
                "status": "SKIPPED",
                "reason": "Signal is not a supported buy or sell.",
            }

        assets = signal.get("asset_mints")
        quotes = signal.get("quote_mints")
        changes = result.get("token_balance_changes")

        if (
            not isinstance(assets, list)
            or len(assets) != 1
            or not isinstance(quotes, list)
            or len(quotes) != 1
            or not isinstance(changes, list)
        ):
            return {
                "status": "SKIPPED",
                "reason": "Only one asset and one quote token are supported.",
            }

        asset_mint, quote_mint = assets[0], quotes[0]
        if (
            not isinstance(asset_mint, str)
            or not isinstance(quote_mint, str)
            or asset_mint == quote_mint
        ):
            return {"status": "SKIPPED", "reason": "Invalid token mints."}

        asset_delta = self._delta_for_mint(changes, asset_mint)
        quote_delta = self._delta_for_mint(changes, quote_mint)

        if asset_delta is None or quote_delta is None:
            return {
                "status": "SKIPPED",
                "reason": "Token amounts are unavailable or invalid.",
            }

        asset_balance = self.balances.get(asset_mint, Decimal("0"))
        quote_balance = self.balances.get(quote_mint, Decimal("0"))

        if action == "BUY_CANDIDATE":
            if asset_delta <= 0 or quote_delta >= 0:
                return {"status": "SKIPPED", "reason": "Buy direction is inconsistent."}

            asset_amount = asset_delta
            quote_amount = -quote_delta
            if quote_balance < quote_amount:
                return {"status": "REJECTED", "reason": "Insufficient paper quote balance."}

            new_asset_balance = asset_balance + asset_amount
            new_quote_balance = quote_balance - quote_amount
            side = "BUY"
        else:
            if asset_delta >= 0 or quote_delta <= 0:
                return {"status": "SKIPPED", "reason": "Sell direction is inconsistent."}

            asset_amount = -asset_delta
            quote_amount = quote_delta
            if asset_balance < asset_amount:
                return {"status": "REJECTED", "reason": "Insufficient paper asset balance."}

            new_asset_balance = asset_balance - asset_amount
            new_quote_balance = quote_balance + quote_amount
            side = "SELL"

        trade = {
            "signature": signature,
            "side": side,
            "asset_mint": asset_mint,
            "asset_amount": str(asset_amount),
            "quote_mint": quote_mint,
            "quote_amount": str(quote_amount),
            "status": "PAPER_FILLED",
        }

        new_balances = dict(self.balances)
        new_balances[asset_mint] = new_asset_balance
        new_balances[quote_mint] = new_quote_balance

        # Persist first, so a database error cannot partially update memory.
        self._persist_trade(trade, new_balances)

        self.balances = new_balances
        self.processed_signatures.add(signature)
        self.trades.append(trade)
        return trade

    def set_balance(self, mint, amount, action="set"):
        """Set or add a virtual token balance and persist it without real transfers."""
        if not isinstance(mint, str) or not mint.strip():
            raise ValueError("A token mint is required.")
        try:
            quantity = Decimal(str(amount))
        except Exception as exc:
            raise ValueError("Invalid virtual balance amount.") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("Virtual balance amount must be positive.")
        if action not in ("set", "add"):
            raise ValueError("Balance action must be set or add.")

        current = self.balances.get(mint, Decimal("0"))
        updated = current + quantity if action == "add" else quantity
        balances = dict(self.balances)
        balances[mint] = updated

        if self.db_path is not None:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO paper_balances (mint, amount)
                        VALUES (?, ?)
                        ON CONFLICT(mint) DO UPDATE SET amount = excluded.amount
                        """,
                        (mint, str(updated)),
                    )

        self.balances = balances
        return {"mint": mint, "balance": str(updated), "action": action}

    def get_balances(self):
        """Return a safe copy of virtual balances as strings."""
        return {mint: str(amount) for mint, amount in self.balances.items()}


if __name__ == "__main__":
    print("PaperTrader module loaded successfully.")