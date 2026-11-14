#!/usr/bin/env bash
# Report whether production startup and all known runtime components have
# migrated away from Python. Report-only by default until cutover is approved.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/Engen-main/Engen-main"
STRICT=0
if [[ "${1:-}" == "--strict" ]]; then STRICT=1; fi

blockers=0
notes=0
report_blocker() {
  printf 'BLOCKER: %s\n' "$1"
  blockers=$((blockers + 1))
}
report_note() {
  printf 'NOTE: %s\n' "$1"
  notes=$((notes + 1))
}

printf '%s\n' 'Rust-only production runtime audit'
printf 'Repository root: %s\n' "$ROOT"

# All tracked non-test Python application/runtime modules are listed here.
# They remain blockers until a Rust equivalent is accepted or the feature is
# explicitly retired and the Python file is moved to reference-only storage.
PYTHON_PRODUCTION_PATHS=(
  "main.py"
  "run_dashboard.py"
  "Engen-main/Engen-main/main.py"
  "Engen-main/Engen-main/run_dashboard.py"
  "Engen-main/Engen-main/dashboard/__main__.py"
  "Engen-main/Engen-main/dashboard/server.py"
  "Engen-main/Engen-main/dashboard/server_with_pending.py"
  "Engen-main/Engen-main/dashboard/monitor_bridge.py"
  "Engen-main/Engen-main/dashboard/monitor_service.py"
  "Engen-main/Engen-main/engine/apply_source_freshness.py"
  "Engen-main/Engen-main/engine/config.py"
  "Engen-main/Engen-main/engine/health.py"
  "Engen-main/Engen-main/engine/latency.py"
  "Engen-main/Engen-main/engine/logger.py"
  "Engen-main/Engen-main/engine/market_data.py"
  "Engen-main/Engen-main/engine/monitor_runner.py"
  "Engen-main/Engen-main/engine/paper_monitor_runner.py"
  "Engen-main/Engen-main/engine/paper_copy_runner.py"
  "Engen-main/Engen-main/engine/paper_simulator.py"
  "Engen-main/Engen-main/engine/paper_trader.py"
  "Engen-main/Engen-main/engine/paper_trading.py"
  "Engen-main/Engen-main/engine/pending_copy_report.py"
  "Engen-main/Engen-main/engine/pnl.py"
  "Engen-main/Engen-main/engine/profile_store.py"
  "Engen-main/Engen-main/engine/rpc_policy.py"
  "Engen-main/Engen-main/engine/safety.py"
  "Engen-main/Engen-main/engine/signature_store.py"
  "Engen-main/Engen-main/engine/solana_public.py"
  "Engen-main/Engen-main/engine/solana_validation.py"
  "Engen-main/Engen-main/engine/source_freshness.py"
  "Engen-main/Engen-main/engine/telegram_bot.py"
  "Engen-main/Engen-main/engine/transaction_analysis.py"
  "Engen-main/Engen-main/engine/transaction_monitor.py"
  "Engen-main/Engen-main/engine/transaction_processor.py"
  "Engen-main/Engen-main/engine/wallet_monitor.py"
  "Engen-main/Engen-main/tools/collect_wallet_transactions.py"
  "Engen-main/Engen-main/tools/evaluate_transaction_labels.py"
  "Engen-main/Engen-main/tools/verify_profile_store_recovery.py"
)

for relative in "${PYTHON_PRODUCTION_PATHS[@]}"; do
  absolute="$ROOT/$relative"
  if [[ -f "$absolute" ]]; then
    report_blocker "Python production path remains: $relative."
  fi
done

README="$APP/README.md"
ROOT_README="$ROOT/README.md"
for file in "$README" "$ROOT_README"; do
  if [[ -f "$file" ]] && grep -nE 'python[^[:cntrl:]]*(dashboard\.server|dashboard/server|main\.py|run_dashboard\.py|engine\.telegram_bot)' "$file"; then
    report_blocker "A user-facing README still documents a Python application launcher: ${file#"$ROOT"/}."
  fi
done

LAUNCHER="$ROOT/scripts/run_engen_rust_dashboard.sh"
if [[ ! -f "$LAUNCHER" ]]; then
  report_blocker "Rust-only dashboard launcher is missing."
elif grep -nE '(^|[[:space:]])python(3)?([[:space:]]|$)' "$LAUNCHER"; then
  report_blocker "Rust dashboard launcher must not invoke Python."
else
  report_note "Rust dashboard launcher exists and contains no Python command."
fi

MAIN="$APP/rust-engine/src/main.rs"
LIB="$APP/rust-engine/src/lib.rs"
if [[ ! -f "$MAIN" ]] || ! grep -q 'serve-dashboard' "$MAIN"; then
  report_blocker "Rust binary does not expose a serve-dashboard production command."
fi
if [[ ! -f "$MAIN" ]] || ! grep -q 'health-check' "$MAIN"; then
  report_blocker "Rust binary does not expose the native health-check command."
fi
if [[ ! -f "$LIB" ]] || ! grep -q 'pub mod dashboard_server' "$LIB"; then
  report_blocker "Rust library does not expose an integrated dashboard-server module."
fi
if [[ ! -f "$LIB" ]] || ! grep -q 'pub mod health' "$LIB"; then
  report_blocker "Rust library does not expose native health checks."
fi

RENDER="$ROOT/render.yaml"
if [[ ! -f "$RENDER" ]] || ! grep -qE '^[[:space:]]+runtime:[[:space:]]*rust[[:space:]]*$' "$RENDER"; then
  report_blocker "Render production service is not configured for the Rust runtime."
fi

if [[ -f "$APP/requirements.txt" ]]; then
  report_note "Python requirements.txt remains for reference/test migration; it must not be used by the Rust production build."
fi

report_note "This is a static inventory gate. Full API parity, data compatibility, restart recovery, and end-to-end evidence remain separate acceptance gates."
printf '\nBlockers: %s | Notes: %s\n' "$blockers" "$notes"
if (( blockers == 0 )); then
  printf '%s\n' 'RUNTIME GATE: PASS (static entry-point checks only; full E2E evidence is still required).'
  exit 0
fi

printf '%s\n' 'RUNTIME GATE: NOT READY — do not remove Python components or claim Rust-only production.'
if (( STRICT == 1 )); then
  exit 1
fi
exit 0
