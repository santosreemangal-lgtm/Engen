
"""Dashboard backend for read-only chain monitoring and paper-only simulation."""

import json
import os
import re
import hmac
import sqlite3
import uuid
import sys
import time
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from decimal import Decimal
from datetime import datetime, timezone

from engine.profile_store import ProfileStore, LABELS, STATUSES, validate_public_address, settings_json

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

# Make the existing engine importable when this file is run directly.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.monitor_bridge import create_scan_function
from dashboard.monitor_service import MonitorService
from engine.logger import redact
from engine.pnl import MarketPrice, realized_pnl_from_fills
from engine.paper_copy_runner import get_paper_simulator, PAPER_SIMULATOR_LOCK as paper_simulator_lock
from engine.paper_simulator import PaperSimulator
from engine.solana_public import get_public_health, SolanaRPCError

WALLETS_FILE = Path(os.environ.get("WALLETS_FILE", str(ROOT / "wallets.json")))
PAPER_SIMULATOR_DB = Path(os.environ.get("PAPER_SIMULATOR_DB", str(PROJECT_ROOT / "data" / "paper_simulator.db")))
PROFILE_STORE_DB = Path(os.environ.get("PROFILE_STORE_DB", str(PROJECT_ROOT / "data" / "profiles.db")))
PROFILE_DATA_DIR = Path(os.environ.get("PROFILE_DATA_DIR", str(PROFILE_STORE_DB.parent / "profiles")))
MAX_HISTORY_PAGE_SIZE = 100
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
TRUST_PROXY = os.environ.get("DASHBOARD_TRUST_PROXY", "").strip().lower() == "true"
MONITOR_STATE_FILE = Path(os.environ.get(
    "MONITOR_STATE_FILE", str(PROFILE_STORE_DB.parent / "monitoring_state.json")
))
_MONITOR_STATE_LOCK = threading.RLock()

ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MAX_SAVED_WALLETS = 100
MAX_REQUEST_BODY_BYTES = 10000
API_RATE_LIMIT = 120
API_RATE_WINDOW_SECONDS = 60
_API_RATE_LOCK = threading.Lock()
_API_REQUEST_TIMES = {}
DASHBOARD_AUTH_TOKEN = os.environ.get("DASHBOARD_AUTH_TOKEN", "")


def validate_server_config(host, auth_token, trust_proxy=False):
    """Allow remote binding only behind a trusted HTTPS proxy with strong API auth."""
    if auth_token and len(auth_token) < 32:
        raise ValueError("DASHBOARD_AUTH_TOKEN must contain at least 32 characters.")
    if host not in ("127.0.0.1", "localhost", "::1"):
        if not trust_proxy or not auth_token or len(auth_token) < 32:
            raise ValueError(
                "Non-localhost binding requires DASHBOARD_TRUST_PROXY=true and "
                "a DASHBOARD_AUTH_TOKEN of at least 32 characters."
            )


def is_valid_solana_address(value):
    """Validate Base58 alphabet and the decoded 32-byte public-key length."""
    if not isinstance(value, str) or not ADDRESS_PATTERN.fullmatch(value):
        return False

    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in value:
        number = number * 58 + alphabet.index(character)

    decoded = bytearray()
    while number:
        number, remainder = divmod(number, 256)
        decoded.append(remainder)
    decoded.reverse()

    leading_ones = len(value) - len(value.lstrip("1"))
    decoded = bytearray(leading_ones) + decoded
    return len(decoded) == 32


# Creating services does not start monitoring or make RPC requests.
monitor_service = MonitorService(create_scan_function(), interval=30)
# Each saved profile gets its own in-memory monitor lifecycle. The saved source
# wallet list remains profile-scoped in ProfileStore.
profile_monitor_services = {}
profile_paper_simulators = {}


def load_monitor_state():
    """Load only explicit monitor opt-ins saved before a process restart."""
    with _MONITOR_STATE_LOCK:
        try:
            value = json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"global_enabled": False, "profile_ids": []}
        if not isinstance(value, dict):
            return {"global_enabled": False, "profile_ids": []}
        ids = value.get("profile_ids", [])
        valid_ids = []
        if isinstance(ids, list):
            for item in ids:
                try:
                    canonical = canonical_profile_id(item)
                except ValueError:
                    continue
                if canonical not in valid_ids:
                    valid_ids.append(canonical)
        return {"global_enabled": value.get("global_enabled") is True, "profile_ids": valid_ids}


def save_monitor_state(state):
    """Atomically persist explicit monitoring opt-ins on durable storage."""
    if not isinstance(state, dict):
        raise ValueError("Monitor state must be an object.")
    profile_ids = state.get("profile_ids", [])
    if not isinstance(profile_ids, list):
        raise ValueError("Monitor profile_ids must be a list.")
    normalized = []
    for item in profile_ids:
        canonical = canonical_profile_id(item)
        if canonical not in normalized:
            normalized.append(canonical)
    clean = {"global_enabled": state.get("global_enabled") is True, "profile_ids": normalized}
    with _MONITOR_STATE_LOCK:
        MONITOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = MONITOR_STATE_FILE.with_suffix(MONITOR_STATE_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        temporary.replace(MONITOR_STATE_FILE)
    return clean


def set_global_monitor_persistence(enabled):
    state = load_monitor_state()
    state["global_enabled"] = enabled is True
    return save_monitor_state(state)


def set_profile_monitor_persistence(profile_id, enabled):
    canonical = canonical_profile_id(profile_id)
    state = load_monitor_state()
    ids = [item for item in state["profile_ids"] if item != canonical]
    if enabled:
        ids.append(canonical)
    state["profile_ids"] = ids
    return save_monitor_state(state)


def restore_persisted_monitors():
    """Resume only monitors that the user explicitly started before a restart."""
    state = load_monitor_state()
    cleaned = {"global_enabled": False, "profile_ids": []}
    if state["global_enabled"]:
        wallets = load_wallets()
        if wallets:
            monitor_service.start(wallets)
            cleaned["global_enabled"] = True
    for profile_id in state["profile_ids"]:
        try:
            profile = get_profile_record(profile_id)
            if profile is None or profile.get("status") != "active":
                continue
            addresses = profile.get("source_wallets", [])
            if not addresses:
                continue
            get_profile_monitor_service(profile_id).start(addresses)
            cleaned["profile_ids"].append(profile_id)
        except (OSError, RuntimeError, ValueError):
            # Keep the server available; a bad/deleted profile must not block boot.
            continue
    save_monitor_state(cleaned)
    return cleaned


def load_wallets():
    if not WALLETS_FILE.exists():
        return []

    try:
        data = json.loads(WALLETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []

    wallets = []
    for address in data:
        if not is_valid_solana_address(address) or address in wallets:
            continue
        wallets.append(address)
        if len(wallets) >= MAX_SAVED_WALLETS:
            break
    return wallets


def save_wallets(wallets):
    temporary = WALLETS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(wallets, indent=2), encoding="utf-8")
    temporary.replace(WALLETS_FILE)



def canonical_profile_id(profile_id):
    """Return a canonical UUID so profile IDs cannot be used as filesystem paths."""
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("A valid profile_id is required.")
    try:
        return str(uuid.UUID(profile_id.strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("A valid profile_id is required.") from exc


def profile_paper_db_path(profile_id):
    """Return the isolated paper-ledger path for one canonical profile UUID."""
    return PROFILE_DATA_DIR / canonical_profile_id(profile_id) / "paper_trading.db"


def get_profile_paper_simulator(profile_id):
    """Return one persistent simulator per profile; never share account balances."""
    canonical = canonical_profile_id(profile_id)
    with paper_simulator_lock:
        simulator = profile_paper_simulators.get(canonical)
        if simulator is not None:
            return simulator
        path = profile_paper_db_path(canonical)
        simulator = PaperSimulator(db_path=path)
        # Migrate balances saved by the earlier profile-ledger implementation.
        # The copy engine's quote ledger uses USD (paper currency), not a real token.
        if "USD" not in simulator.balances:
            try:
                with sqlite3.connect(str(path), timeout=10) as db:
                    tables = {row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    if "paper_balances" in tables:
                        old = dict(db.execute("SELECT mint,amount FROM paper_balances"))
                        legacy_amount = old.get("USD") or old.get("USDT")
                        if legacy_amount is not None:
                            simulator.set_virtual_balance(legacy_amount, "USD")
                            db.execute(
                                "INSERT INTO paper_balances(mint,amount) VALUES('USD',?) "
                                "ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount",
                                (str(legacy_amount),),
                            )
                            db.commit()
            except (OSError, sqlite3.Error, ValueError):
                # Keep the simulator readable; the balance UI will show blank
                # rather than claim the legacy amount migrated successfully.
                pass
        profile_paper_simulators[canonical] = simulator
        return simulator


def initialize_profile_paper_ledger(profile_id):
    """Create an empty, isolated virtual ledger for an existing profile; never seed funds."""
    profile_id = canonical_profile_id(profile_id)
    if not PROFILE_STORE_DB.is_file():
        raise ValueError("Profile database is unavailable.")
    with sqlite3.connect("file:" + str(PROFILE_STORE_DB.resolve()) + "?mode=ro", uri=True) as db:
        profile = db.execute(
            "SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
            (profile_id,),
        ).fetchone()
    if profile is None:
        raise ValueError("Profile not found.")
    path = profile_paper_db_path(profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=10) as db:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("""CREATE TABLE IF NOT EXISTS paper_balances(
            mint TEXT PRIMARY KEY, amount TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS paper_trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signature TEXT NOT NULL UNIQUE, side TEXT NOT NULL,
            asset_mint TEXT NOT NULL, asset_amount TEXT NOT NULL,
            quote_mint TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_created ON paper_trades(created_at)")
        db.commit()
    return {"profile_id": profile_id, "database_available": True, "initialized": True,
            "balances": {}, "orders": [], "fills": [],
            "real_trading": False, "transaction_broadcast": False}


def set_profile_virtual_balance(profile_id, amount):
    """Set or replace a profile's virtual USD cash balance."""
    from decimal import Decimal, InvalidOperation
    profile_id = canonical_profile_id(profile_id)
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Enter a valid virtual balance amount.") from exc
    if not value.is_finite() or value <= 0 or value > Decimal("1000000000"):
        raise ValueError("Virtual balance must be greater than zero and at most 1,000,000,000 USD.")
    initialize_profile_paper_ledger(profile_id)
    with paper_simulator_lock:
        simulator = get_profile_paper_simulator(profile_id)
        result = simulator.set_virtual_balance(format(value, "f"), "USD")
        # Keep the earlier balance table in sync for safe migration/diagnostics.
        path = profile_paper_db_path(profile_id)
        with sqlite3.connect(str(path), timeout=10) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute(
                "INSERT INTO paper_balances(mint,amount) VALUES('USD',?) "
                "ON CONFLICT(mint) DO UPDATE SET amount=excluded.amount",
                (format(value, "f"),),
            )
            db.execute("DELETE FROM paper_balances WHERE mint='USDT'")
            db.commit()
    return {"profile_id": profile_id, "mint": "USD", "amount": format(value, "f"),
            "balance_updated": True, "real_trading": False, "transaction_broadcast": False}

def load_profile_paper_simulation_report(profile_id):
    """Return the selected profile's isolated paper simulator, balances and history."""
    profile_id = canonical_profile_id(profile_id)
    base = {
        "mode": "PAPER_SIMULATION", "simulated": True,
        "real_wallet_balances_included": False, "real_trading": False,
        "transaction_broadcast": False, "profile_id": profile_id,
        "database_available": False, "balances": {}, "orders": [], "fills": [],
        "available_balance": None, "available_balance_usd": None,
        "initial_balance": None, "portfolio_value_usd": None,
        "realized_pnl_usd": None, "unrealized_pnl_usd": None,
        "pnl": {
            "cost_basis_method": "average_cost", "currency": "USD",
            "realized_by_mint": {},
            "unrealized_status": "unavailable_no_verified_fresh_market_price",
        },
    }
    if not PROFILE_STORE_DB.is_file():
        base["error"] = "Profile database is unavailable."
        return base
    try:
        with sqlite3.connect("file:" + str(PROFILE_STORE_DB.resolve()) + "?mode=ro", uri=True) as db:
            profile = db.execute(
                "SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                (profile_id,),
            ).fetchone()
        if profile is None:
            base["error"] = "Profile not found."
            return base
        path = profile_paper_db_path(profile_id)
        if not path.is_file():
            return base
        with paper_simulator_lock:
            report = get_profile_paper_simulator(profile_id).report()
        base["balances"] = report.get("balances", {})
        base["available_balance"] = base["balances"].get("USD")
        base["available_balance_usd"] = base["available_balance"]
        base["initial_balance"] = report.get("initial_balance")
        base["orders"] = [
            {**order, "simulated": True, "real_trading": False, "transaction_broadcast": False}
            for order in report.get("orders", [])
        ]
        base["fills"] = report.get("fills", [])
        base["pnl"] = report.get("pnl", base["pnl"])
        realized = base["pnl"].get("realized_by_mint", {})
        if not realized:
            base["realized_pnl_usd"] = "0"
        elif all(item.get("status") == "calculated" and item.get("pnl") is not None
                 for item in realized.values()):
            base["realized_pnl_usd"] = format(
                sum((Decimal(item["pnl"]) for item in realized.values()), Decimal("0")), "f")
        else:
            base["realized_pnl_usd"] = None
        has_unpriced_holdings = any(
            mint != "USD" and Decimal(str(amount)) != 0
            for mint, amount in base["balances"].items()
        )
        # With no unpriced token holdings, cash is the known portfolio value.
        # Once tokens are held, a verified fresh price is required to value them.
        base["portfolio_value_usd"] = None if has_unpriced_holdings else base["available_balance"]
        base["unrealized_pnl_usd"] = None if has_unpriced_holdings else "0"
        base["database_available"] = True
        base["real_trading"] = False
        base["transaction_broadcast"] = False
    except (OSError, sqlite3.Error, ValueError) as exc:
        base["error"] = "Profile paper report unavailable (" + type(exc).__name__ + ")."
    return base

def load_paper_simulation_report(profile_id=None):
    """Read paper simulation data only; profile IDs never fall back to global data."""
    if profile_id is not None:
        return load_profile_paper_simulation_report(profile_id)
    """Read the legacy paper-simulator database only; never create or mutate it."""
    base = {
        "mode": "PAPER_SIMULATION",
        "simulated": True,
        "real_wallet_balances_included": False,
        "real_trading": False,
        "transaction_broadcast": False,
        "database_available": PAPER_SIMULATOR_DB.is_file(),
        "balances": {},
        "orders": [],
        "fills": [],
        "pnl": {
            "cost_basis_method": "average_cost",
            "currency": "USD",
            "realized_by_mint": {},
            "unrealized_status": "unavailable_no_verified_fresh_market_price",
        },
    }
    if not PAPER_SIMULATOR_DB.is_file():
        return base
    try:
        import sqlite3
        with sqlite3.connect("file:" + str(PAPER_SIMULATOR_DB.resolve()) + "?mode=ro", uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"simulator_balances", "simulated_orders", "simulated_fills"}.issubset(tables):
                base["error"] = "Simulation database schema is not initialized."
                return base
            base["balances"] = {mint: amount for mint, amount in db.execute(
                "SELECT mint, amount FROM simulator_balances ORDER BY mint")}
            meta_table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='simulator_meta'"
            ).fetchone()
            if meta_table:
                initial = db.execute(
                    "SELECT value FROM simulator_meta WHERE key='initial_balance'"
                ).fetchone()
                base["initial_balance"] = initial[0] if initial else None
            base["orders"] = [
                {"order_id": row[0], "signal_id": row[1], "side": row[2],
                 "asset_mint": row[3], "quantity": row[4], "quote_mint": row[5],
                 "status": row[6], "reason": row[7], "created_at": row[8]}
                for row in db.execute("""SELECT order_id,signal_id,side,asset_mint,quantity,
                    quote_mint,status,reason,created_at FROM simulated_orders ORDER BY created_at DESC LIMIT 100""")
            ]
            base["fills"] = [
                {"fill_id": row[0], "order_id": row[1], "asset_mint": row[2],
                 "quantity": row[3], "reference_price": row[4], "execution_price": row[5],
                 "gross_quote": row[6], "fee": row[7], "network_cost": row[8],
                 "net_quote": row[9], "currency": row[10], "price_source": row[11],
                 "price_timestamp": row[12], "price_age_seconds": row[13],
                 "slippage_bps": row[14], "created_at": row[15]}
                for row in db.execute("""SELECT fill_id,order_id,asset_mint,quantity,reference_price,
                    execution_price,gross_quote,fee,network_cost,net_quote,currency,price_source,
                    price_timestamp,price_age_seconds,slippage_bps,created_at
                    FROM simulated_fills ORDER BY created_at DESC LIMIT 100""")
            ]
            all_fills = [
                {"side": row[0], "asset_mint": row[1], "quantity": row[2],
                 "execution_price": row[3], "fee": row[4], "network_cost": row[5],
                 "created_at": row[6]}
                for row in db.execute("""SELECT o.side,f.asset_mint,f.quantity,f.execution_price,
                    f.fee,f.network_cost,f.created_at FROM simulated_fills f
                    JOIN simulated_orders o ON o.order_id=f.order_id
                    ORDER BY f.rowid""")
            ]
            base["pnl"]["realized_by_mint"] = realized_pnl_from_fills(all_fills)
    except (OSError, sqlite3.Error, ValueError) as exc:
        base["error"] = "Simulation report unavailable (" + type(exc).__name__ + ")."
    return base



def rpc_health_report():
    """Perform an independent, bounded read-only RPC health probe."""
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        status = get_public_health(timeout=5)
        return {
            "mode": "READ_ONLY",
            "available": status == "ok",
            "status": status,
            "endpoint": "configured public Solana RPC",
            "checked_at": checked_at,
            "transaction_broadcast": False,
        }
    except (SolanaRPCError, OSError, TimeoutError) as exc:
        return {
            "mode": "READ_ONLY",
            "available": False,
            "status": "unavailable",
            "endpoint": "configured public Solana RPC",
            "checked_at": checked_at,
            "error": type(exc).__name__,
            "transaction_broadcast": False,
        }


def load_profiles_report():
    """Read existing profile data without creating or modifying the database."""
    base = {"database_available": PROFILE_STORE_DB.is_file(), "profiles": []}
    if not PROFILE_STORE_DB.is_file():
        return base
    try:
        import sqlite3
        with sqlite3.connect("file:" + str(PROFILE_STORE_DB.resolve()) + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"profiles", "profile_settings", "profile_source_wallets", "balance_snapshots"}
            if not required.issubset(tables):
                base["error"] = "Profile database schema is not initialized."
                return base
            rows = db.execute("""SELECT profile_id,name,destination_public_address,status,
                created_at,updated_at FROM profiles
                WHERE deleted_at IS NULL AND status != 'archived'
                ORDER BY created_at,profile_id""").fetchall()
            for row in rows:
                profile = dict(row)
                settings = db.execute("SELECT settings_json FROM profile_settings WHERE profile_id=?",
                                      (profile["profile_id"],)).fetchone()
                profile["settings"] = json.loads(settings["settings_json"]) if settings else {}
                profile["source_wallets"] = [item[0] for item in db.execute(
                    """SELECT wallet_address FROM profile_source_wallets
                       WHERE profile_id=? ORDER BY wallet_address""", (profile["profile_id"],))]
                balances = []
                for balance in db.execute("""SELECT wallet_address,token_mint,raw_amount,token_decimals,
                    observed_at,source,freshness_seconds FROM balance_snapshots
                    WHERE profile_id=? AND is_current=1 ORDER BY token_mint,wallet_address""",
                    (profile["profile_id"],)):
                    item = dict(balance)
                    item["amount"] = format(Decimal(item["raw_amount"]) / (Decimal(10) ** item["token_decimals"]), "f")
                    try:
                        observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                        item["age_seconds"] = max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))
                    except (ValueError, AttributeError):
                        item["age_seconds"] = None
                    item["is_stale"] = (
                        None if item["age_seconds"] is None or item["freshness_seconds"] is None
                        else item["age_seconds"] > item["freshness_seconds"]
                    )
                    item["value_kind"] = "OBSERVED_ON_CHAIN_SNAPSHOT"
                    item["simulated"] = False
                    balances.append(item)
                profile["balances"] = balances
                profile["balances_source"] = "stored snapshots; not a live RPC refresh"
                profile["simulated"] = False
                base["profiles"].append(profile)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        base["error"] = "Profile report unavailable (" + type(exc).__name__ + ")."
    return base


def get_profile_record(profile_id):
    """Read one non-deleted profile using the dashboard's read-only profile report."""
    canonical = canonical_profile_id(profile_id)
    report = load_profiles_report()
    if not report.get("database_available"):
        return None
    return next((item for item in report.get("profiles", [])
                 if item.get("profile_id") == canonical), None)


def profile_copy_side(profile_id):
    """Return a validated profile copy-side mode; default to BOTH for legacy profiles."""
    profile = get_profile_record(profile_id)
    if profile is None:
        return "NONE"
    settings = profile.get("settings") or {}
    paper_settings = settings.get("paper_trading") if isinstance(settings, dict) else {}
    side = paper_settings.get("copy_side", "BOTH") if isinstance(paper_settings, dict) else "BOTH"
    return side.upper() if isinstance(side, str) and side.upper() in ("BUY", "SELL", "BOTH") else "NONE"


def profile_paper_settings(profile_id):
    """Return stored paper-trading settings for one profile."""
    profile = get_profile_record(profile_id)
    if profile is None:
        return {}
    settings = profile.get("settings") or {}
    paper = settings.get("paper_trading") if isinstance(settings, dict) else {}
    return paper if isinstance(paper, dict) else {}


def get_profile_monitor_service(profile_id):
    """Return a dedicated monitor service for a validated profile UUID."""
    canonical = canonical_profile_id(profile_id)
    if canonical not in profile_monitor_services:
        profile_monitor_services[canonical] = MonitorService(
            create_scan_function(
                side_getter=lambda: profile_copy_side(canonical),
                settings_getter=lambda: profile_paper_settings(canonical),
            ),
            interval=30,
        )
    return profile_monitor_services[canonical]


def profile_monitor_report(profile_id):
    """Return monitoring status for one profile only."""
    canonical = canonical_profile_id(profile_id)
    profile = get_profile_record(canonical)
    if profile is None:
        raise ValueError("Profile not found.")
    service = profile_monitor_services.get(canonical)
    status = service.status() if service is not None else {
        "enabled": False, "interval_seconds": 30, "wallets": [],
    }
    return {"profile_id": canonical, "profile_status": profile["status"], "monitor": status}


def load_profile_transactions(query):
    """Return bounded, profile-scoped transaction history with allowlisted filters."""
    if not PROFILE_STORE_DB.is_file():
        return {"database_available": False, "transactions": [], "page": 1,
                "page_size": 25, "total": 0, "pages": 0}
    profile_id = (query.get("profile_id") or [""])[0].strip()
    if not profile_id or len(profile_id) > 64:
        raise ValueError("A valid profile_id is required.")
    def positive_int(name, default, maximum):
        raw = (query.get(name) or [str(default)])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(name + " must be an integer.") from exc
        if value < 1 or value > maximum:
            raise ValueError(name + " is outside the allowed range.")
        return value
    page = positive_int("page", 1, 1000000)
    page_size = positive_int("page_size", 25, MAX_HISTORY_PAGE_SIZE)
    classification = (query.get("classification") or [""])[0].strip()
    status = (query.get("status") or [""])[0].strip()
    token_mint = (query.get("token_mint") or [""])[0].strip()
    if classification and classification not in LABELS:
        raise ValueError("Unsupported transaction classification.")
    if status and status not in STATUSES:
        raise ValueError("Unsupported transaction status.")
    if token_mint:
        validate_public_address(token_mint, "token mint")
    filters = ["p.profile_id=?", "pr.deleted_at IS NULL"]
    params = [profile_id]
    if classification:
        filters.append("t.classification=?")
        params.append(classification)
    if status:
        filters.append("t.status=?")
        params.append(status)
    if token_mint:
        filters.append("t.token_mint=?")
        params.append(token_mint)
    where = " AND ".join(filters)
    import sqlite3
    try:
        with sqlite3.connect("file:" + str(PROFILE_STORE_DB.resolve()) + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"transactions", "profile_transactions", "profiles"}.issubset(tables):
                return {"database_available": True, "transactions": [], "page": page,
                        "page_size": page_size, "total": 0, "pages": 0,
                        "error": "Transaction history schema is not initialized."}
            if not db.execute("SELECT 1 FROM profiles WHERE profile_id=? AND deleted_at IS NULL",
                              (profile_id,)).fetchone():
                raise ValueError("Profile not found.")
            total = db.execute("""SELECT COUNT(*) FROM transactions t
                JOIN profile_transactions p ON p.signature=t.signature
                JOIN profiles pr ON pr.profile_id=p.profile_id
                WHERE """ + where, params).fetchone()[0]
            offset = (page - 1) * page_size
            rows = db.execute("""SELECT t.signature,t.source_wallet_address,t.slot,t.block_time,
                t.observed_at,t.status,t.classification,t.evidence_json,t.raw_amount,
                t.token_mint,t.token_decimals FROM transactions t
                JOIN profile_transactions p ON p.signature=t.signature
                JOIN profiles pr ON pr.profile_id=p.profile_id
                WHERE """ + where + """
                ORDER BY t.observed_at DESC,t.signature LIMIT ? OFFSET ?""",
                params + [page_size, offset]).fetchall()
            transactions = []
            for row in rows:
                item = dict(row)
                try:
                    item["evidence"] = json.loads(item.pop("evidence_json"))
                except json.JSONDecodeError:
                    item["evidence"] = {"classification_note": "Stored evidence could not be parsed."}
                item["amount"] = None
                if item["raw_amount"] is not None and item["token_decimals"] is not None:
                    item["amount"] = format(
                        Decimal(item["raw_amount"]) / (Decimal(10) ** item["token_decimals"]), "f")
                item["value_kind"] = "OBSERVED_TRANSACTION_METADATA"
                item["simulated"] = False
                transactions.append(item)
            return {"database_available": True, "transactions": transactions, "page": page,
                    "page_size": page_size, "total": total,
                    "pages": (total + page_size - 1) // page_size}
    except sqlite3.Error as exc:
        raise RuntimeError("Transaction history is temporarily unavailable (" + type(exc).__name__ + ").") from exc



class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # Apply baseline browser protections to API responses and static assets.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        super().end_headers()

    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.send_json(415, {"error": "Content-Type must be application/json"})
            return None

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_REQUEST_BODY_BYTES:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid JSON request"})
            return None

        if not isinstance(payload, dict):
            self.send_json(400, {"error": "A JSON object is required"})
            return None

        return payload

    def _api_rate_limited(self):
        now = time.monotonic()
        client = self.client_address[0]
        with _API_RATE_LOCK:
            recent = [stamp for stamp in _API_REQUEST_TIMES.get(client, []) if now - stamp < API_RATE_WINDOW_SECONDS]
            if len(recent) >= API_RATE_LIMIT:
                _API_REQUEST_TIMES[client] = recent
                return True
            recent.append(now)
            _API_REQUEST_TIMES[client] = recent
            if len(_API_REQUEST_TIMES) > 1000:
                stale_clients = [
                    address for address, stamps in _API_REQUEST_TIMES.items()
                    if not stamps or now - stamps[-1] >= API_RATE_WINDOW_SECONDS
                ]
                for address in stale_clients:
                    _API_REQUEST_TIMES.pop(address, None)
        return False

    def _api_authorized(self):
        if not DASHBOARD_AUTH_TOKEN:
            return True
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not supplied.startswith(prefix):
            return False
        return hmac.compare_digest(supplied[len(prefix):], DASHBOARD_AUTH_TOKEN)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/") and self._api_rate_limited():
            self.send_json(429, {"error": "API request limit reached. Wait before retrying."})
            return
        if path.startswith("/api/") and not self._api_authorized():
            self.send_json(401, {"error": "Dashboard API authentication required."})
            return

        if path == "/api/wallets":
            self.send_json(200, {"wallets": load_wallets()})
            return

        if path == "/api/monitor":
            self.send_json(200, monitor_service.status())
            return

        if path == "/api/rpc-health":
            report = rpc_health_report()
            self.send_json(200, report)
            return

        if path == "/api/paper-simulation":
            profile_id = (parse_qs(urlparse(self.path).query).get("profile_id") or [None])[0]
            try:
                report = load_paper_simulation_report(profile_id)
                if profile_id is not None and report.get("error") == "Profile not found.":
                    self.send_json(404, {"error": "Profile not found."})
                else:
                    self.send_json(200, report)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            return

        if path == "/api/profile-monitor":
            query = parse_qs(urlparse(self.path).query)
            profile_id = (query.get("profile_id") or [""])[0]
            try:
                self.send_json(200, profile_monitor_report(profile_id))
            except ValueError as exc:
                status = 404 if str(exc) == "Profile not found." else 400
                self.send_json(status, {"error": str(exc)})
            except OSError as exc:
                self.send_json(503, {"error": "Monitor state storage unavailable (" + type(exc).__name__ + ")."})
            return

        if path == "/api/profiles":
            report = load_profiles_report()
            self.send_json(200, report)
            return

        if path == "/api/profile-transactions":
            try:
                report = load_profile_transactions(parse_qs(urlparse(self.path).query))
                self.send_json(200, report)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.send_json(503, {"error": str(exc)})
            return

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/") and self._api_rate_limited():
            self.send_json(429, {"error": "API request limit reached. Wait before retrying."})
            return
        if path.startswith("/api/") and not self._api_authorized():
            self.send_json(401, {"error": "Dashboard API authentication required."})
            return
        origin = self.headers.get("Origin")
        if path.startswith("/api/") and origin:
            parsed_origin = urlparse(origin)
            if parsed_origin.scheme not in ("http", "https") or parsed_origin.netloc.lower() != self.headers.get("Host", "").lower():
                self.send_json(403, {"error": "Cross-origin state changes are not allowed."})
                return
        payload = self.read_json()

        if payload is None:
            return

        if path == "/api/paper-simulation":
            action = payload.get("action")
            if action == "initialize-profile-ledger":
                try:
                    result = initialize_profile_paper_ledger(payload.get("profile_id"))
                    self.send_json(200, result)
                except ValueError as exc:
                    status = 404 if str(exc) == "Profile not found." else 400
                    self.send_json(status, {"error": str(exc)})
                except (OSError, sqlite3.Error) as exc:
                    self.send_json(503, {"error": "Profile paper ledger storage unavailable (" + type(exc).__name__ + ")."})
                return
            if action == "set-profile-initial-balance":
                try:
                    result = set_profile_virtual_balance(payload.get("profile_id"), payload.get("amount"))
                    self.send_json(200, {"result": result, "report": load_profile_paper_simulation_report(payload.get("profile_id"))})
                except ValueError as exc:
                    status = 404 if str(exc) == "Profile not found." else 400
                    self.send_json(status, {"error": str(exc)})
                except (OSError, sqlite3.Error) as exc:
                    self.send_json(503, {"error": "Profile paper ledger storage unavailable (" + type(exc).__name__ + ")."})
                return
            try:
                with paper_simulator_lock:
                    simulator = get_paper_simulator()
                    if action == "set-initial-balance":
                        result = simulator.set_initial_virtual_balance(
                            payload.get("amount"), payload.get("mint", "USD")
                        )
                        self.send_json(200, {"result": result, "report": dict(simulator.report(), database_available=True)})
                        return
                    if action == "add-balance":
                        result = simulator.add_virtual_balance(
                            payload.get("mint"), payload.get("amount")
                        )
                        self.send_json(200, {"result": result, "report": dict(simulator.report(), database_available=True)})
                        return
                    if action == "create-order":
                        side = payload.get("side")
                        asset_mint = payload.get("asset_mint")
                        quote_mint = payload.get("quote_mint", "USD")
                        now = datetime.now(timezone.utc).isoformat()
                        if not isinstance(asset_mint, str) or not asset_mint.strip() or len(asset_mint.strip()) > 128:
                            raise ValueError("Enter an asset mint or paper asset symbol (max 128 characters).")
                        if not isinstance(quote_mint, str) or not quote_mint.strip() or len(quote_mint.strip()) > 128:
                            raise ValueError("Enter a quote mint or paper quote symbol (max 128 characters).")
                        if asset_mint.strip() == quote_mint.strip():
                            raise ValueError("Asset and quote assets must be different.")
                        if quote_mint.strip().upper() != "USD":
                            raise ValueError("This paper simulator currently supports USD as the quote currency only.")
                        market_price = MarketPrice(
                            mint=asset_mint.strip(),
                            price=payload.get("price"),
                            currency="USD",
                            source="user-entered-unverified",
                            observed_at=now,
                            liquidity_usd=payload.get("liquidity_usd"),
                            liquidity_source="user-entered-unverified",
                            liquidity_observed_at=now,
                        )
                        order = simulator.create_order(
                            payload.get("signal_id") or str(uuid.uuid4()),
                            side,
                            asset_mint.strip(),
                            payload.get("quantity"),
                            quote_mint.strip(),
                            created_at=now,
                        )
                        if order.get("status") == "DUPLICATE":
                            self.send_json(409, {"error": "This paper order signal was already processed."})
                            return
                        result = simulator.execute_order(
                            order["order_id"], market_price, now=now,
                            max_price_age_seconds=60, slippage_bps=50, fee_bps=10,
                            min_liquidity_usd=10000, max_liquidity_age_seconds=60,
                            require_liquidity=True,
                        )
                        self.send_json(200, {"result": result, "report": dict(simulator.report(), database_available=True)})
                        return
                    self.send_json(400, {"error": "Action must be set-initial-balance, add-balance, or create-order."})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except (OSError, sqlite3.Error) as exc:
                self.send_json(503, {"error": "Paper simulation storage unavailable (" + type(exc).__name__ + ")."})
            return

        if path == "/api/profile-monitor":
            action = payload.get("action")
            try:
                profile_id = canonical_profile_id(payload.get("profile_id"))
                profile = get_profile_record(profile_id)
                if profile is None:
                    self.send_json(404, {"error": "Profile not found."})
                    return
                if action == "start":
                    if profile.get("status") != "active":
                        self.send_json(409, {"error": "Activate this profile before starting monitoring."})
                        return
                    addresses = profile.get("source_wallets", [])
                    if not addresses:
                        self.send_json(400, {"error": "Associate at least one source public wallet with this profile first."})
                        return
                    service = get_profile_monitor_service(profile_id)
                    status = service.start(addresses)
                    try:
                        set_profile_monitor_persistence(profile_id, True)
                    except OSError:
                        service.stop()
                        raise
                    self.send_json(200, {"profile_id": profile_id, "monitor": status})
                    return
                if action == "stop":
                    set_profile_monitor_persistence(profile_id, False)
                    service = profile_monitor_services.get(profile_id)
                    status = service.stop() if service is not None else {
                        "enabled": False, "interval_seconds": 30, "wallets": [],
                    }
                    self.send_json(200, {"profile_id": profile_id, "monitor": status})
                    return
                self.send_json(400, {"error": "Action must be start or stop."})
            except ValueError as exc:
                status = 404 if str(exc) == "Profile not found." else 400
                self.send_json(status, {"error": str(exc)})
            return

        if path == "/api/profiles":
            action = payload.get("action", "create")
            try:
                store = ProfileStore(PROFILE_STORE_DB)
                if action == "create":
                    profile_id = store.create_profile(
                        payload.get("name"),
                        destination_public_address=payload.get("destination_public_address"),
                        settings=payload.get("settings", {}),
                    )
                    self.send_json(201, {"profile": store.get_profile(profile_id), "simulated": False})
                    return
                profile_id = payload.get("profile_id")
                if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 64:
                    raise ValueError("A valid profile_id is required.")
                if action == "update":
                    # Validate settings before changing profile details so invalid
                    # secret-like fields cannot leave a partially applied update.
                    if "settings" in payload:
                        settings_json(payload["settings"])
                    profile = store.update_profile(
                        profile_id,
                        name=payload.get("name"),
                        destination_public_address=payload.get("destination_public_address"),
                        status=payload.get("status"),
                    )
                    if "settings" in payload:
                        store.set_settings(profile_id, payload["settings"])
                        profile = store.get_profile(profile_id)
                    self.send_json(200, {"profile": profile, "simulated": False})
                    return
                if action == "settings":
                    store.set_settings(profile_id, payload.get("settings"))
                    self.send_json(200, {"profile": store.get_profile(profile_id), "simulated": False})
                    return
                if action == "associate-source-wallet":
                    store.associate_source_wallet(profile_id, payload.get("wallet_address"))
                    self.send_json(200, {"profile": store.get_profile(profile_id), "simulated": False})
                    return
                if action == "disassociate-source-wallet":
                    store.disassociate_source_wallet(profile_id, payload.get("wallet_address"))
                    self.send_json(200, {"profile": store.get_profile(profile_id), "simulated": False})
                    return
                if action == "delete":
                    service = profile_monitor_services.get(profile_id)
                    if service is not None:
                        service.stop_and_wait(timeout=15)
                        profile_monitor_services.pop(profile_id, None)
                    set_profile_monitor_persistence(profile_id, False)
                    store.delete_profile(profile_id)
                    self.send_json(200, {"deleted": True, "profile_id": profile_id, "history_preserved": True})
                    return
                raise ValueError("Action must be create, update, settings, associate-source-wallet, disassociate-source-wallet, or delete.")
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except (OSError, RuntimeError) as exc:
                self.send_json(503, {"error": "Profile storage unavailable (" + type(exc).__name__ + ")."})
            return

        if path == "/api/monitor":
            action = payload.get("action")

            if action == "start":
                wallets = load_wallets()
                if not wallets:
                    self.send_json(
                        400,
                        {"error": "Add at least one wallet before starting."},
                    )
                    return

                try:
                    set_global_monitor_persistence(True)
                    status = monitor_service.start(wallets)
                except OSError as exc:
                    monitor_service.stop()
                    self.send_json(503, {"error": "Monitor state storage unavailable (" + type(exc).__name__ + ")."})
                    return
                self.send_json(200, status)
                return

            if action == "stop":
                try:
                    set_global_monitor_persistence(False)
                except OSError as exc:
                    self.send_json(503, {"error": "Monitor state storage unavailable (" + type(exc).__name__ + ")."})
                    return
                status = monitor_service.stop()
                self.send_json(200, status)
                return

            self.send_json(
                400,
                {"error": "Action must be start or stop"},
            )
            return

        if path != "/api/wallets":
            self.send_json(404, {"error": "Endpoint not found"})
            return

        address = payload.get("address")
        action = payload.get("action", "add")

        if (
            not isinstance(address, str)
            or not is_valid_solana_address(address.strip())
        ):
            self.send_json(400, {"error": "Invalid-looking Solana address"})
            return

        address = address.strip()
        wallets = load_wallets()

        if action == "add":
            if address not in wallets:
                if len(wallets) >= MAX_SAVED_WALLETS:
                    self.send_json(
                        413,
                        {"error": "The saved-wallet limit has been reached."},
                    )
                    return
                wallets.append(address)
                save_wallets(wallets)

        elif action == "remove":
            wallets = [item for item in wallets if item != address]
            save_wallets(wallets)

        else:
            self.send_json(
                400,
                {"error": "Action must be add or remove"},
            )
            return

        self.send_json(200, {"wallets": wallets})

    def log_message(self, format_string, *args):
        message = format_string % args
        print("%s - %s" % (self.address_string(), redact(message)))


if __name__ == "__main__":
    print("Solana Monitor dashboard backend starting at %s:%s" % (HOST, PORT))
    print("Wallet storage: %s" % WALLETS_FILE)
    print("Monitoring is opt-in; no scan starts automatically.")
    print("Read-only mode: no trades, signing, or private keys.")
    try:
        validate_server_config(HOST, DASHBOARD_AUTH_TOKEN, TRUST_PROXY)
    except ValueError as exc:
        raise SystemExit(
            "%s For remote access, use a trusted HTTPS reverse proxy with "
            "authentication and keep this backend bound to localhost." % exc
        ) from exc

    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    try:
        resumed = restore_persisted_monitors()
        print("Restored explicit monitoring opt-ins: %s" % resumed)
    except (OSError, RuntimeError, ValueError) as exc:
        print("WARNING: persisted monitoring could not be restored (%s)." % type(exc).__name__)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard backend stopping.")
    finally:
        shutdown_status = monitor_service.stop_and_wait(timeout=15)
        if not shutdown_status["worker_stopped"]:
            print("WARNING: monitor worker did not stop within the shutdown timeout.")
        server.server_close()
        print("Dashboard backend stopped.")
