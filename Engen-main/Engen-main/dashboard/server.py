
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
from engine.logger import redact

WALLETS_FILE = ROOT / "wallets.json"
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

        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self.read_json()

        if payload is None:
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
