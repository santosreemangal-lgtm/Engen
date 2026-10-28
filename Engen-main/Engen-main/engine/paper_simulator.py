"""Deterministic paper-only order simulator. Contains no RPC or transaction-signing code."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from engine.pnl import MarketPrice, decimal_value, parse_timestamp


class PaperSimulator:
    """Persist simulated orders/fills; never broadcasts or signs transactions."""

    def __init__(self, initial_balances=None, db_path=None):
        self.db_path = Path(db_path) if db_path is not None else None
        self.balances = {}
        for mint, amount in (initial_balances or {}).items():
            if not isinstance(mint, str) or not mint.strip():
                raise ValueError("Balance token mint is required.")
            self.balances[mint] = decimal_value(amount, "Initial balance", allow_zero=True)
        self.orders = {}
        self.fills = []
        self.transactions = []
        self.processed_signals = set()
        self._initial_balance_value = None
        self._initial_balance_mint = None
        self._rebuild_pnl_ledger()
        if self.db_path:
            self._initialize()
            self._initial_balance_value = self._stored_initial_balance()
            if self._initial_balance_value is not None:
                self._initial_balance_mint = "USD"

    def _connect(self):
        return sqlite3.connect(str(self.db_path))

    def _initialize(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            with db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("""CREATE TABLE IF NOT EXISTS simulator_meta(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS simulator_balances(
                    mint TEXT PRIMARY KEY, amount TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS simulated_orders(
                    order_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE,
                    side TEXT NOT NULL, asset_mint TEXT NOT NULL, quantity TEXT NOT NULL,
                    quote_mint TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                    created_at TEXT NOT NULL, details_json TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS simulated_fills(
                    fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES simulated_orders(order_id),
                    asset_mint TEXT NOT NULL, quantity TEXT NOT NULL, reference_price TEXT NOT NULL,
                    execution_price TEXT NOT NULL, gross_quote TEXT NOT NULL, fee TEXT NOT NULL,
                    network_cost TEXT NOT NULL, net_quote TEXT NOT NULL, currency TEXT NOT NULL,
                    price_source TEXT NOT NULL, price_timestamp TEXT NOT NULL,
                    price_age_seconds INTEGER NOT NULL, slippage_bps INTEGER NOT NULL,
                    created_at TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS simulator_transactions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL, mint TEXT NOT NULL, amount TEXT NOT NULL,
                    balance_after TEXT NOT NULL, created_at TEXT NOT NULL)""")
                initialized = db.execute("SELECT value FROM simulator_meta WHERE key='initialized'").fetchone()
                if initialized is None:
                    db.executemany("INSERT INTO simulator_balances VALUES(?,?)",
                                   [(m, str(a)) for m, a in self.balances.items()])
                    db.execute("INSERT INTO simulator_meta VALUES('initialized','1')")
                else:
                    self.balances = {m: Decimal(a) for m, a in
                                     db.execute("SELECT mint,amount FROM simulator_balances")}
                for row in db.execute("SELECT order_id,signal_id,side,asset_mint,quantity,quote_mint,status,reason,created_at,details_json FROM simulated_orders"):
                    self.orders[row[0]] = {
                        "order_id": row[0], "signal_id": row[1], "side": row[2],
                        "asset_mint": row[3], "quantity": row[4], "quote_mint": row[5],
                        "status": row[6], "reason": row[7], "created_at": row[8],
                        "details": json.loads(row[9]),
                    }
                self.processed_signals = {o["signal_id"] for o in self.orders.values()}
                fill_rows = db.execute("""SELECT f.fill_id,f.order_id,o.side,f.asset_mint,f.quantity,
                    o.quote_mint,f.reference_price,f.execution_price,f.gross_quote,f.fee,
                    f.network_cost,f.net_quote,f.currency,f.price_source,f.price_timestamp,
                    f.price_age_seconds,f.slippage_bps,f.created_at
                    FROM simulated_fills f JOIN simulated_orders o ON o.order_id=f.order_id
                    ORDER BY f.rowid""").fetchall()
                self.fills = [dict(zip(
                    ("fill_id","order_id","side","asset_mint","quantity","quote_mint",
                     "reference_price","execution_price","gross_quote","fee","network_cost",
                     "net_quote","currency","price_source","price_timestamp","price_age_seconds",
                     "slippage_bps","created_at"), row)) for row in fill_rows]
                tx_rows = db.execute("""SELECT id,kind,mint,amount,balance_after,created_at
                    FROM simulator_transactions ORDER BY id""").fetchall()
                self.transactions = [
                    {"id": row[0], "kind": row[1], "mint": row[2], "amount": row[3],
                     "balance_after": row[4], "created_at": row[5], "simulated": True}
                    for row in tx_rows
                ]
        self._rebuild_pnl_ledger()

    def _persist(self, order, balances=None, fill=None):
        if self.db_path is None:
            return
        with closing(self._connect()) as db:
            with db:
                db.execute("""INSERT INTO simulated_orders VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,
                    details_json=excluded.details_json""",
                    (order["order_id"],order["signal_id"],order["side"],order["asset_mint"],
                     order["quantity"],order["quote_mint"],order["status"],order.get("reason"),
                     order["created_at"],json.dumps(order.get("details",{}),sort_keys=True)))
                if balances is not None:
                    db.executemany("""INSERT INTO simulator_balances VALUES(?,?)
                    ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount""",
                    [(m,str(a)) for m,a in balances.items()])
                if fill is not None:
                    db.execute("""INSERT INTO simulated_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        tuple(fill[k] for k in ("fill_id","order_id","asset_mint","quantity","reference_price",
                        "execution_price","gross_quote","fee","network_cost","net_quote","currency",
                        "price_source","price_timestamp","price_age_seconds","slippage_bps","created_at")))

    def create_order(self, signal_id, side, asset_mint, quantity, quote_mint, *, created_at, details=None):
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise ValueError("Signal ID is required for deduplication.")
        if signal_id in self.processed_signals:
            return {"status": "DUPLICATE", "signal_id": signal_id}
        if side not in {"BUY", "SELL"}:
            raise ValueError("Side must be BUY or SELL.")
        if not isinstance(asset_mint,str) or not asset_mint or not isinstance(quote_mint,str) or not quote_mint or asset_mint == quote_mint:
            raise ValueError("Distinct asset and quote mints are required.")
        qty = decimal_value(quantity, "Order quantity")
        parse_timestamp(created_at)
        order = {"order_id": str(uuid.uuid4()), "signal_id": signal_id, "side": side,
                 "asset_mint": asset_mint, "quantity": str(qty), "quote_mint": quote_mint,
                 "status": "CREATED", "reason": None, "created_at": created_at, "details": {
                     "mode": "PAPER_SIMULATION", "real_trading": False, "transaction_broadcast": False,
                     **(details if isinstance(details, dict) else {})}}
        self._persist(order)
        self.orders[order["order_id"]] = order
        self.processed_signals.add(signal_id)
        return dict(order)

    def set_initial_virtual_balance(self, amount, mint="USD"):
        """Set the starting paper balance exactly once before any simulated fills."""
        if not isinstance(mint, str) or not mint.strip() or len(mint.strip()) > 128:
            raise ValueError("A token mint or paper asset symbol (max 128 characters) is required.")
        mint = mint.strip()
        value = decimal_value(amount, "Initial virtual balance", allow_zero=True)
        stored_initial_balance = self._stored_initial_balance()
        if stored_initial_balance is None:
            stored_initial_balance = self._initial_balance_value
        if stored_initial_balance is not None:
            if Decimal(stored_initial_balance) != value or self._initial_balance_mint != mint:
                raise ValueError("Starting balance has already been set and cannot be changed.")
            if self.balances.get(mint) != value:
                raise ValueError("The stored starting balance does not match this asset balance.")
            return {"mint": mint, "initial_balance": str(value), "balance": str(value),
                    "simulated": True, "real_trading": False, "transaction_broadcast": False}
        if any(order.get("status") == "FILLED" for order in self.orders.values()):
            raise ValueError("Starting balance cannot be changed after a paper order has filled.")
        self._save_balance_change(
            "INITIAL_BALANCE", mint, value, value, set_initial_balance=True
        )
        return {"mint": mint, "initial_balance": str(value), "balance": str(value),
                "simulated": True, "real_trading": False, "transaction_broadcast": False}

    def _save_balance_change(self, kind, mint, amount, balance_after, *, set_initial_balance=False):
        """Persist a virtual balance mutation and its audit event together."""
        created_at = datetime.now(timezone.utc).isoformat()
        event = {
            "kind": kind, "mint": mint, "amount": str(amount),
            "balance_after": str(balance_after), "created_at": created_at,
            "simulated": True,
        }
        if self.db_path is not None:
            with closing(self._connect()) as db:
                with db:
                    db.execute(
                        """INSERT INTO simulator_balances(mint, amount) VALUES(?, ?)
                        ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount""",
                        (mint, str(balance_after)),
                    )
                    if set_initial_balance:
                        db.execute(
                            """INSERT INTO simulator_meta(key, value) VALUES('initial_balance', ?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                            (str(amount),),
                        )
                    cursor = db.execute(
                        """INSERT INTO simulator_transactions
                        (kind,mint,amount,balance_after,created_at) VALUES(?,?,?,?,?)""",
                        (kind, mint, str(amount), str(balance_after), created_at),
                    )
                    event["id"] = cursor.lastrowid
        else:
            event["id"] = len(self.transactions) + 1
        self.balances[mint] = balance_after
        if set_initial_balance:
            self._initial_balance_value = str(amount)
            self._initial_balance_mint = mint
        self.transactions.append(event)
        return event

    def add_virtual_balance(self, mint, amount):
        """Add explicitly virtual funds to one paper token balance; never touches a real wallet."""
        if not isinstance(mint, str) or not mint.strip() or len(mint.strip()) > 128:
            raise ValueError("A token mint or paper asset symbol (max 128 characters) is required.")
        mint = mint.strip()
        value = decimal_value(amount, "Virtual funding amount")
        updated = self.balances.get(mint, Decimal("0")) + value
        self._save_balance_change("DEPOSIT", mint, value, updated)
        return {"mint": mint, "added": str(value), "balance": str(updated),
                "simulated": True, "real_trading": False,
                "transaction_broadcast": False}

    def cancel_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("Order not found.")
        if order["status"] != "CREATED":
            return {"status": "REJECTED", "reason": "Only unexecuted orders can be cancelled."}
        updated = dict(order, status="CANCELLED", reason="Cancelled before simulated execution.")
        self._persist(updated)
        self.orders[order_id] = updated
        return dict(updated)

    def execute_order(self, order_id, market_price, *, now, max_price_age_seconds=60,
                      slippage_bps=50, fee_bps=10, network_cost=0, rpc_ok=True,
                      min_liquidity_usd=10000, max_liquidity_age_seconds=60,
                      require_liquidity=True):
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("Order not found.")
        if order["status"] != "CREATED":
            return {"status": "REJECTED", "reason": "Order is not awaiting execution."}
        if not isinstance(rpc_ok, bool) or not rpc_ok:
            return self._reject(order, "RPC/provider data unavailable; no fill created.")
        if not isinstance(market_price, MarketPrice) or market_price.mint != order["asset_mint"]:
            return self._reject(order, "Market price unavailable or for a different token.")
        if not market_price.is_fresh(max_age_seconds=max_price_age_seconds, now=now):
            return self._reject(order, "Market price is stale; no fill created.")
        if not isinstance(require_liquidity, bool):
            raise ValueError("require_liquidity must be boolean.")
        minimum_liquidity = decimal_value(
            min_liquidity_usd, "Minimum USD liquidity", allow_zero=True
        )
        if (not isinstance(max_liquidity_age_seconds, int)
                or isinstance(max_liquidity_age_seconds, bool)
                or max_liquidity_age_seconds < 0):
            raise ValueError("max_liquidity_age_seconds must be a non-negative integer.")
        if market_price.liquidity_usd is None:
            if require_liquidity:
                return self._reject(order, "Liquidity data unavailable; no fill created.")
        else:
            liquidity_age = market_price.liquidity_age_seconds(now)
            if liquidity_age > max_liquidity_age_seconds:
                return self._reject(order, "Liquidity data is stale; no fill created.")
            if market_price.liquidity_usd < minimum_liquidity:
                return self._reject(order, "Liquidity is below the configured USD minimum; no fill created.")
        for name, value in (("slippage_bps",slippage_bps),("fee_bps",fee_bps)):
            if not isinstance(value,int) or isinstance(value,bool) or not 0 <= value <= 10000:
                raise ValueError(f"{name} must be an integer from 0 to 10000.")
        qty = Decimal(order["quantity"])
        target_notional = (order.get("details") or {}).get("target_notional_usd")
        if target_notional is not None:
            target_value = decimal_value(target_notional, "Target USD paper order size")
            if target_value <= 0:
                return self._reject(order, "Target USD paper order size must be positive.")
            qty = target_value / market_price.price
            if qty <= 0:
                return self._reject(order, "Target USD paper order size produced an invalid quantity.")
            order["quantity"] = str(qty)
            self._persist(order)
        reference = market_price.price
        multiplier = Decimal(10000 + slippage_bps if order["side"] == "BUY" else 10000 - slippage_bps) / Decimal(10000)
        execution = reference * multiplier
        gross = qty * execution
        fee = gross * Decimal(fee_bps) / Decimal(10000)
        network = decimal_value(network_cost, "Network cost", allow_zero=True)
        net = gross + fee + network if order["side"] == "BUY" else gross - fee - network
        if net < 0:
            return self._reject(order, "Costs exceed simulated sale proceeds.")
        balances = dict(self.balances)
        asset_balance = balances.get(order["asset_mint"], Decimal("0"))
        quote_balance = balances.get(order["quote_mint"], Decimal("0"))
        if order["side"] == "BUY":
            if quote_balance < net:
                return self._reject(order, "Insufficient simulated quote balance.")
            balances[order["quote_mint"]] = quote_balance - net
            balances[order["asset_mint"]] = asset_balance + qty
        else:
            if asset_balance < qty:
                return self._reject(order, "Insufficient simulated asset balance.")
            balances[order["asset_mint"]] = asset_balance - qty
            balances[order["quote_mint"]] = quote_balance + net
        fill = {"fill_id":str(uuid.uuid4()),"order_id":order_id,"asset_mint":order["asset_mint"],
                "quantity":str(qty),"reference_price":str(reference),"execution_price":str(execution),
                "gross_quote":str(gross),"fee":str(fee),"network_cost":str(network),"net_quote":str(net),
                "currency":"USD","price_source":market_price.source,"price_timestamp":market_price.observed_at,
                "price_age_seconds":market_price.age_seconds(now),"slippage_bps":slippage_bps,
                "created_at":now}
        updated = dict(order, status="FILLED", reason=None, details={**order["details"],
                       "execution_state":"FILLED",
                       "reference_price":str(reference),"execution_price":str(execution),
                       "fee":str(fee),"network_cost":str(network),"fill_id":fill["fill_id"]})
        self._persist(updated, balances=balances, fill=fill)
        self.orders[order_id] = updated
        self.balances = balances
        fill["side"] = order["side"]
        fill["quote_mint"] = order["quote_mint"]
        self.fills.append(fill)
        self._rebuild_pnl_ledger()
        return {"status":"FILLED","simulated":True,"real_trading":False,
                "transaction_broadcast":False,"order":dict(updated),"fill":dict(fill)}

    def _reject(self, order, reason):
        updated = dict(order, status="REJECTED", reason=reason,
                       details={**order["details"], "execution_state":"REJECTED"})
        self._persist(updated)
        self.orders[order["order_id"]] = updated
        return {"status":"REJECTED","reason":reason,"real_trading":False,"transaction_broadcast":False}

    def _rebuild_pnl_ledger(self):
        """Recalculate realized P&L from persisted fills in stable order."""
        from engine.pnl import AverageCostLedger
        ledger = AverageCostLedger()
        events = []
        for fill in self.fills:
            mint = fill["asset_mint"]
            quantity = fill["quantity"]
            price = fill["execution_price"]
            fee = fill["fee"]
            network = fill["network_cost"]
            if fill["side"] == "BUY":
                event = ledger.buy(mint, quantity, price, fee=fee, network_fee=network,
                                   timestamp=fill["created_at"])
            else:
                position = ledger.snapshot(mint)
                tracked = Decimal(position["quantity"])
                sold = Decimal(quantity)
                if sold > tracked:
                    # Initial virtual assets can lack acquisition history. Mark
                    # that missing basis unknown rather than assuming zero cost.
                    ledger.deposit(mint, sold - tracked, unit_price=None,
                                   timestamp=fill["created_at"])
                event = ledger.sell(mint, quantity, price, fee=fee, network_fee=network,
                                    timestamp=fill["created_at"])
            events.append(event)
        self.pnl_ledger = ledger
        self.pnl_events = events

    def pnl_summary(self):
        by_mint = {}
        for event in self.pnl_events:
            if event["type"] != "SELL":
                continue
            mint = event["mint"]
            item = by_mint.setdefault(mint, {"realized_pnl": Decimal("0"), "complete": True,
                                               "currency": "USD"})
            if event["realized_pnl"] is None:
                item["complete"] = False
            else:
                item["realized_pnl"] += Decimal(event["realized_pnl"])
        realized = {
            mint: {"pnl": str(item["realized_pnl"]) if item["complete"] else None,
                   "status": "calculated" if item["complete"] else "unknown_cost_basis",
                   "currency": item["currency"]}
            for mint, item in sorted(by_mint.items())
        }
        return {"cost_basis_method": "average_cost", "currency": "USD",
                "realized_by_mint": realized,
                "unrealized_status": "unavailable_no_verified_fresh_market_price",
                "unrealized_note": "No verified market-price provider is connected to the simulator."}

    def _stored_initial_balance(self):
        if self.db_path is None or not self.db_path.is_file():
            return None
        try:
            with closing(self._connect()) as db:
                row = db.execute("SELECT value FROM simulator_meta WHERE key='initial_balance'").fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def report(self):
        return {"mode":"PAPER_SIMULATION","simulated":True,"real_wallet_balances_included":False,
                "real_trading":False,"transaction_broadcast":False,
                "balances":{mint:str(amount) for mint,amount in self.balances.items()},
                "initial_balance": self._stored_initial_balance(),
                "orders":[dict(order) for order in self.orders.values()],
                "fills":[dict(fill) for fill in self.fills],
                "transactions":[dict(event) for event in self.transactions],
                "pnl":self.pnl_summary()}
