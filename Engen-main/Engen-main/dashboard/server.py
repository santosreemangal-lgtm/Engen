
"""Read-only web backend for the Solana Monitor dashboard."""

import json
import os
import re
import sys
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
from engine.pnl import realized_pnl_from_fills
from engine.solana_public import get_public_health, SolanaRPCError

WALLETS_FILE = ROOT / "wallets.json"
PAPER_SIMULATOR_DB = Path(os.environ.get("PAPER_SIMULATOR_DB", str(PROJECT_ROOT / "data" / "paper_simulator.db")))
PROFILE_STORE_DB = Path(os.environ.get("PROFILE_STORE_DB", str(PROJECT_ROOT / "data" / "profiles.db")))
MAX_HISTORY_PAGE_SIZE = 100
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 8000

ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MAX_SAVED_WALLETS = 100
MAX_REQUEST_BODY_BYTES = 10000


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


# Creating the service does not start monitoring or make RPC requests.
monitor_service = MonitorService(create_scan_function(), interval=30)


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



def load_paper_simulation_report():
    """Read the paper-simulator database only; never create or mutate it."""
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

    def do_GET(self):
        path = urlparse(self.path).path

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
            self.send_json(200, load_paper_simulation_report())
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
        payload = self.read_json()

        if payload is None:
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
                if action == "delete":
                    store.delete_profile(profile_id)
                    self.send_json(200, {"deleted": True, "profile_id": profile_id, "history_preserved": True})
                    return
                raise ValueError("Action must be create, update, settings, associate-source-wallet, or delete.")
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

                self.send_json(
                    200,
                    monitor_service.start(wallets),
                )
                return

            if action == "stop":
                self.send_json(200, monitor_service.stop())
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
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(
            "WARNING: dashboard has no authentication. Exposing it beyond "
            "localhost may let other users change saved wallets and control "
            "read-only monitoring. Use only behind a trusted authenticated proxy."
        )

    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

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
