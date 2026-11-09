"""Canonical paper dashboard entry: pending_copy on API + UI injection.

Replaces the need to hand-edit large server.py / index.html files.

- Monkey-patches paper report loaders to attach pending_copy
- Serves index.html with pending_copy_ui.js injected before </body>
- Activates stale-source gate for paper-copy queueing

Paper only: no keys, no signing, no broadcast.

Usage:
  python run_dashboard.py
  python -m dashboard
  python dashboard/server_with_pending.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import dashboard.server as server
from engine.pending_copy_report import attach_pending_copy
import engine.apply_source_freshness  # noqa: F401  # activates stale-source gate

_orig_profile = server.load_profile_paper_simulation_report
_orig_global = server.load_paper_simulation_report

INJECT_SCRIPT = '<script src="/pending_copy_ui.js"></script>'


def load_profile_paper_simulation_report(profile_id):
    report = _orig_profile(profile_id)
    try:
        attach_pending_copy(
            report,
            simulator=server.get_profile_paper_simulator(profile_id),
        )
    except Exception:
        attach_pending_copy(report, simulator=None)
    return report


def load_paper_simulation_report(profile_id=None):
    report = _orig_global(profile_id)
    if profile_id is not None:
        return report
    try:
        attach_pending_copy(report, simulator=server.get_paper_simulator())
    except Exception:
        attach_pending_copy(report, simulator=None)
    return report


server.load_profile_paper_simulation_report = load_profile_paper_simulation_report
server.load_paper_simulation_report = load_paper_simulation_report


class PendingDashboardHandler(server.DashboardHandler):
    """Serve index with pending-copy UI script; keep all API routes unchanged."""

    def do_GET(self):
        from urllib.parse import urlparse

        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            index_path = Path(self.translate_path("/index.html"))
            try:
                body = index_path.read_text(encoding="utf-8")
            except OSError:
                return super().do_GET()
            if "pending_copy_ui.js" not in body:
                if "</body>" in body:
                    body = body.replace(
                        "</body>", INJECT_SCRIPT + "\n</body>", 1
                    )
                else:
                    body = body + "\n" + INJECT_SCRIPT + "\n"
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()


def main():
    print(
        "Solana Monitor dashboard backend starting at %s:%s"
        % (server.HOST, server.PORT)
    )
    print("Wallet storage: %s" % server.WALLETS_FILE)
    print("Pending-copy API + UI injection: enabled")
    print("Stale-source paper-copy gate: enabled (60s window)")
    print("Monitoring is opt-in; no scan starts automatically.")
    print("Read-only mode: no trades, signing, or private keys.")
    try:
        server.validate_server_config(
            server.HOST, server.DASHBOARD_AUTH_TOKEN, server.TRUST_PROXY
        )
    except ValueError as exc:
        raise SystemExit(
            "%s For remote access, use a trusted HTTPS reverse proxy with "
            "authentication and keep this backend bound to localhost." % exc
        ) from exc

    httpd = server.ThreadingHTTPServer(
        (server.HOST, server.PORT), PendingDashboardHandler
    )
    try:
        resumed = server.restore_persisted_monitors()
        print("Restored explicit monitoring opt-ins: %s" % resumed)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            "WARNING: persisted monitoring could not be restored (%s)."
            % type(exc).__name__
        )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard backend stopping.")
    finally:
        shutdown_status = server.monitor_service.stop_and_wait(timeout=15)
        if not shutdown_status["worker_stopped"]:
            print(
                "WARNING: monitor worker did not stop within the shutdown timeout."
            )
        httpd.server_close()
        print("Dashboard backend stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
