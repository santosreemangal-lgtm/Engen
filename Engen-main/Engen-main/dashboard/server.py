
"""Read-only web backend for the Solana Monitor dashboard."""

import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

# Make the existing engine importable when this file is run directly.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.monitor_bridge import create_scan_function
from dashboard.monitor_service import MonitorService
from engine.paper_trading import PaperTrader, PaperTradingError
from engine.paper_trader import PaperTrader as CopyPaperTrader

WALLETS_FILE = ROOT / "wallets.json"
COPY_SETTINGS_FILE = ROOT / "copy_settings.json"
PAPER_DB_FILE = ROOT / "paper_copy_trading.db"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
# Bind to localhost by default because this dashboard has no authentication.
# Set DASHBOARD_HOST=0.0.0.0 only when using a trusted, access-controlled
# environment such as a private development container.
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 8000

ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# The manual dashboard ledger remains separate for compatibility.
paper_trader = PaperTrader()
# Monitored-wallet copies use a persistent virtual-only ledger.
copy_paper_trader = CopyPaperTrader(
    {USDT_MINT: Decimal("10000")},
    db_path=PAPER_DB_FILE,
)


def load_copy_settings():
    defaults = {"mode": "percentage", "value": "100"}
    try:
        settings = json.loads(COPY_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(settings, dict):
        return defaults
    mode, value = settings.get("mode"), settings.get("value")
    if mode not in ("percentage", "fixed") or not isinstance(value, (str, int, float)):
        return defaults
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return defaults
    if not amount.is_finite() or amount <= 0:
        return defaults
    if mode == "percentage" and amount > 100:
        return defaults
    if mode == "fixed" and amount > Decimal("10000"):
        return defaults
    return {"mode": mode, "value": str(amount)}


def save_copy_settings(settings):
    temporary = COPY_SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(COPY_SETTINGS_FILE)


def handle_monitored_result(wallet_address, result):
    """Convert a supported public-wallet signal into a virtual paper trade."""
    if not isinstance(result, dict):
        return {"status": "SKIPPED", "reason": "Invalid transaction result."}
    signal = result.get("trade_signal")
    if not isinstance(signal, dict):
        return {"status": "SKIPPED", "reason": "No classified trade signal."}
    if signal.get("classification") not in ("BUY_CANDIDATE", "SELL_CANDIDATE"):
        return {"status": "SKIPPED", "reason": "Signal is not a buy/sell candidate."}

    assets, quotes = signal.get("asset_mints"), signal.get("quote_mints")
    changes = result.get("token_balance_changes")
    if (
        not isinstance(assets, list) or len(assets) != 1
        or not isinstance(quotes, list) or len(quotes) != 1
        or not isinstance(changes, list)
    ):
        return {"status": "SKIPPED", "reason": "Only one asset and one quote token are supported."}

    asset_mint, quote_mint = assets[0], quotes[0]
    if (
        not isinstance(asset_mint, str) or not isinstance(quote_mint, str)
        or asset_mint == quote_mint
    ):
        return {"status": "SKIPPED", "reason": "Invalid token pair."}
    if quote_mint != USDT_MINT:
        return {"status": "SKIPPED", "reason": "Only USDT-quoted swaps are supported by the current virtual balance."}

    asset_delta = CopyPaperTrader._delta_for_mint(changes, asset_mint)
    quote_delta = CopyPaperTrader._delta_for_mint(changes, quote_mint)
    if asset_delta is None or quote_delta is None or quote_delta == 0:
        return {"status": "SKIPPED", "reason": "Token amounts are unavailable or invalid."}

    def decimals_for(mint):
        values = {
            item.get("decimals") for item in changes
            if isinstance(item, dict) and item.get("mint") == mint
        }
        if len(values) != 1:
            return None
        value = next(iter(values))
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        return value

    asset_places, quote_places = decimals_for(asset_mint), decimals_for(quote_mint)
    if asset_places is None or quote_places is None:
        return {"status": "SKIPPED", "reason": "Token decimals are unavailable or inconsistent."}

    settings = load_copy_settings()
    setting_value = Decimal(settings["value"])
    if settings["mode"] == "percentage":
        scale = setting_value / Decimal("100")
    else:
        # quote_delta is in human USDT units, so this scales the observed
        # swap to the configured fixed USDT amount (not raw integer units).
        scale = setting_value / abs(quote_delta)

    if not scale.is_finite() or scale <= 0:
        return {"status": "SKIPPED", "reason": "Copy size is invalid."}

    # Consolidate each token to one raw-unit change before scaling. This avoids
    # independently rounding multiple token accounts and accidentally changing
    # the ratio or dropping small-but-valid swaps.
    scaled_changes = []
    for mint, places, human_delta in (
        (asset_mint, asset_places, asset_delta),
        (quote_mint, quote_places, quote_delta),
    ):
        raw_delta = human_delta * (Decimal("10") ** places)
        scaled_raw = (raw_delta * scale).to_integral_value(rounding=ROUND_DOWN)
        if scaled_raw == 0:
            return {"status": "SKIPPED", "reason": "Copy size is below token precision."}
        scaled_changes.append({
            "mint": mint,
            "delta_amount": str(int(scaled_raw)),
            "decimals": places,
        })

    copied_result = dict(result)
    copied_result["token_balance_changes"] = scaled_changes
    outcome = copy_paper_trader.process(copied_result)
    return {
        "wallet": wallet_address,
        "signature": result.get("signature"),
        "paper_result": outcome,
        "real_trading": False,
    }


# Monitoring is opt-in; constructing the service does not scan or trade.
monitor_service = MonitorService(
    create_scan_function(),
    interval=30,
    on_result=handle_monitored_result,
)


def load_wallets():
    if not WALLETS_FILE.exists():
        return []

    try:
        data = json.loads(WALLETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []

    return [
        address
        for address in data
        if isinstance(address, str)
        and ADDRESS_PATTERN.fullmatch(address)
    ]


def save_wallets(wallets):
    temporary = WALLETS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(wallets, indent=2), encoding="utf-8")
    temporary.replace(WALLETS_FILE)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

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
            if not 1 <= length <= 10000:
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

        if path == "/api/paper/status":
            self.send_json(200, paper_trader.status())
            return

        if path == "/api/paper/history":
            self.send_json(200, {"history": paper_trader.history()})
            return

        if path == "/api/copy/settings":
            self.send_json(200, load_copy_settings())
            return

        if path == "/api/copy/status":
            self.send_json(200, {
                "mode": "paper",
                "real_trading": False,
                "balances": copy_paper_trader.get_balances(),
                "trade_count": len(copy_paper_trader.trades),
                "history": list(reversed(copy_paper_trader.trades[-100:])),
                "settings": load_copy_settings(),
            })
            return

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self.read_json()

        if payload is None:
            return

        if path == "/api/copy/balance":
            action = payload.get("action", "add")
            amount = payload.get("amount")
            try:
                updated = copy_paper_trader.set_balance(
                    USDT_MINT,
                    amount,
                    action=action,
                )
            except (ValueError, InvalidOperation) as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(200, {
                **updated,
                "balances": copy_paper_trader.get_balances(),
                "real_trading": False,
            })
            return

        if path == "/api/copy/settings":
            mode = payload.get("mode")
            value = payload.get("value")
            if mode not in ("percentage", "fixed"):
                self.send_json(400, {"error": "Mode must be percentage or fixed."})
                return
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                self.send_json(400, {"error": "Copy value must be a valid number."})
                return
            if (
                not amount.is_finite()
                or amount <= 0
                or (mode == "percentage" and amount > 100)
                or (mode == "fixed" and amount > Decimal("10000"))
            ):
                self.send_json(400, {"error": "Percentage must be 0-100; fixed USDT amount must be greater than 0 and no more than 10000."})
                return
            settings = {"mode": mode, "value": str(amount)}
            save_copy_settings(settings)
            self.send_json(200, settings)
            return

        if path == "/api/paper/control":
            action = payload.get("action")
            if action == "pause":
                self.send_json(200, paper_trader.pause())
                return
            if action == "resume":
                self.send_json(200, paper_trader.resume())
                return
            self.send_json(400, {"error": "Action must be pause or resume"})
            return

        if path == "/api/paper/trade":
            try:
                side = payload.get("side")
                if side == "buy":
                    result = paper_trader.buy(
                        payload.get("token"), payload.get("quantity"), payload.get("price")
                    )
                elif side == "sell":
                    result = paper_trader.sell(
                        payload.get("token"), payload.get("quantity"), payload.get("price")
                    )
                else:
                    self.send_json(400, {"error": "Side must be buy or sell"})
                    return
            except PaperTradingError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_json(200, {"trade": result, "status": paper_trader.status()})
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
            or not ADDRESS_PATTERN.fullmatch(address.strip())
        ):
            self.send_json(400, {"error": "Invalid-looking Solana address"})
            return

        address = address.strip()
        wallets = load_wallets()

        if action == "add":
            if address not in wallets:
                wallets.append(address)
                save_wallets(wallets)
            if monitor_service.status()["enabled"]:
                monitor_service.start(wallets)

        elif action == "remove":
            wallets = [item for item in wallets if item != address]
            save_wallets(wallets)
            if monitor_service.status()["enabled"]:
                if wallets:
                    monitor_service.start(wallets)
                else:
                    monitor_service.stop()

        else:
            self.send_json(
                400,
                {"error": "Action must be add or remove"},
            )
            return

        self.send_json(200, {"wallets": wallets})

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))


if __name__ == "__main__":
    print("Solana Monitor dashboard backend starting on port %s" % PORT)
    print("Wallet storage: %s" % WALLETS_FILE)
    print("Monitoring is opt-in; no scan starts automatically.")
    print("Read-only mode: no trades, signing, or private keys.")

    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard backend stopping.")
    finally:
        monitor_service.stop()
        server.server_close()
        print("Dashboard backend stopped.")
