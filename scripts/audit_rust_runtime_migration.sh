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

SERVER="$APP/dashboard/server.py"
if [[ -f "$SERVER" ]]; then
  report_blocker "Python dashboard server still exists at Engen-main/Engen-main/dashboard/server.py."
  if grep -nE '^from dashboard\.(monitor_bridge|monitor_service)|^from engine\.(paper_copy_runner|paper_simulator|pnl|solana_public|market_data)' "$SERVER"; then
    report_note "The dashboard entry point imports Python monitoring, paper-execution, P&L, RPC or market-data components."
  fi
else
  report_note "Python dashboard server file is absent."
fi

README="$APP/README.md"
ROOT_README="$ROOT/README.md"
for file in "$README" "$ROOT_README"; do
  if [[ -f "$file" ]] && grep -nE 'python -m dashboard\.server|python[^[:alnum:]]+dashboard/server\.py' "$file"; then
    report_blocker "A user-facing README still documents Python as the dashboard launcher: ${file#"$ROOT"/}."
  fi
done

MAIN="$APP/rust-engine/src/main.rs"
LIB="$APP/rust-engine/src/lib.rs"
if [[ ! -f "$MAIN" ]] || ! grep -q 'serve-dashboard' "$MAIN"; then
  report_blocker "Rust binary does not expose a serve-dashboard production command."
fi
if [[ ! -f "$LIB" ]] || ! grep -q 'pub mod dashboard_server' "$LIB"; then
  report_blocker "Rust library does not expose an integrated dashboard-server module."
fi

for module in \
  "$APP/dashboard/monitor_bridge.py" \
  "$APP/dashboard/monitor_service.py" \
  "$APP/engine/paper_simulator.py" \
  "$APP/engine/paper_copy_runner.py"; do
  if [[ -f "$module" ]]; then
    report_note "Python source still present (must be proven reference-only or removed after parity): ${module#"$ROOT"/}."
  fi
done

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
