
"""Read-only web backend for the Solana Monitor dashboard."""

import json
import os
import re
import sys
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

WALLETS_FILE = ROOT / "wallets.json"
# Bind to localhost by default because this dashboard has no authentication.
# Set DASHBOARD_HOST=0.0.0.0 only when using a trusted, access-controlled
# environment such as a private development container.
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = 8000

ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Creating the service does not start monitoring or make RPC requests.
monitor_service = MonitorService(create_scan_function(), interval=30)
# Deliberately in-memory and virtual-only; restart resets this demo ledger.
paper_trader = PaperTrader()


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
