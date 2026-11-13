#!/usr/bin/env bash
# Report whether production startup has been migrated away from Python.
# This is intentionally report-only by default while migration is in progress.
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

# Every known Python entry point or module still reached by the legacy
# dashboard/Telegram/health-check runtime is a production blocker until
# migrated or moved to an isolated reference-only location.
PYTHON_PRODUCTION_PATHS=(
  "main.py"
  "Engen-main/Engen-main/main.py"
  "Engen-main/Engen-main/dashboard/server.py"
  "Engen-main/Engen-main/dashboard/monitor_bridge.py"
  "Engen-main/Engen-main/dashboard/monitor_service.py"
  "Engen-main/Engen-main/engine/telegram_bot.py"
  "Engen-main/Engen-main/engine/paper_trading.py"
  "Engen-main/Engen-main/engine/paper_simulator.py"
  "Engen-main/Engen-main/engine/paper_copy_runner.py"
  "Engen-main/Engen-main/engine/profile_store.py"
  "Engen-main/Engen-main/engine/pnl.py"
  "Engen-main/Engen-main/engine/market_data.py"
  "Engen-main/Engen-main/engine/solana_public.py"
  "Engen-main/Engen-main/engine/rpc_policy.py"
  "Engen-main/Engen-main/engine/signature_store.py"
  "Engen-main/Engen-main/engine/solana_validation.py"
  "Engen-main/Engen-main/engine/transaction_analysis.py"
  "Engen-main/Engen-main/engine/transaction_monitor.py"
  "Engen-main/Engen-main/engine/transaction_processor.py"
  "Engen-main/Engen-main/engine/wallet_monitor.py"
  "Engen-main/Engen-main/engine/config.py"
  "Engen-main/Engen-main/engine/health.py"
  "Engen-main/Engen-main/engine/logger.py"
)

for relative in "\${PYTHON_PRODUCTION_PATHS[@]}"; do
  absolute="$ROOT/$relative"
  if [[ -f "$absolute" ]]; then
    report_blocker "Python production path remains: $relative."
  fi
done

README="$APP/README.md"
ROOT_README="$ROOT/README.md"
for file in "$README" "$ROOT_README"; do
  if [[ -f "$file" ]] && grep -nE 'python -m dashboard\.server|python[^[:alnum:]]+dashboard/server\.py|python[^[:alnum:]]+main\.py|python[^[:alnum:]]+engine\.telegram_bot' "$file"; then
    report_blocker "A user-facing README still documents a Python production launcher: \${file#"$ROOT"/}."
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
if [[ ! -f "$LIB" ]] || ! grep -q 'pub mod dashboard_server' "$LIB"; then
  report_blocker "Rust library does not expose an integrated dashboard-server module."
fi

if [[ -f "$APP/requirements.txt" ]]; then
  report_note "Python requirements.txt remains; remove or split it only after all Python production consumers are migrated."
fi

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
