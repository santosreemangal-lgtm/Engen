"""Dashboard entry with pending-copy report wiring.

Same dashboard as server.py, but paper-simulation reports include pending_copy.
Paper-only: no signing, no broadcast.

Usage:
  python -m dashboard.server_with_pending
  # or from Engen-main/Engen-main:
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

_orig_profile = server.load_profile_paper_simulation_report
_orig_global = server.load_paper_simulation_report


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
    # When profile_id is set, orig_global delegates to the (patched) profile loader.
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


def main():
    print(
        "Solana Monitor dashboard backend starting at %s:%s"
        % (server.HOST, server.PORT)
    )
    print("Wallet storage: %s" % server.WALLETS_FILE)
    print("Pending-copy report wiring: enabled")
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
        (server.HOST, server.PORT), server.DashboardHandler
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
