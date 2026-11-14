# Current-head update — 2026-11-14

Step 1 core application/domain component parity has been accepted on draft branch `migration/rust-dashboard-runtime-20261113`, verified code commit `1f38d3373b8e193bb1f279733bf7ef6a5c2815b3`.
- [CI push run #640](https://github.com/santosreemangal-lgtm/Engen/actions/runs/43571357136): success.
- [CI PR run #641](https://github.com/santosreemangal-lgtm/Engen/actions/runs/43571360458): success.
- [PR #49](https://github.com/santosreemangal-lgtm/Engen/pull/49) remains Draft and unmerged.

The verified code includes Rust-native Dashboard server foundation, monitor/transaction/store components, Rust paper-copy/simulator/P&L, persistent legacy PaperTrader compatibility, health, logging/redaction, source freshness configuration, market data and latency parity. The workflow proves Rust tests, read-only RPC, current Python reference suites, local Dashboard process E2E, no-Python Rust release runtime, Clippy and migration audit execution on that commit.

**This does not mean the whole migration is complete.** The static migration audit still reports 38 Python production/runtime paths. Step 2 full API contract parity, Step 3 integrated monitor/paper/DB service acceptance, Step 4 all CLI/utility/deployment entrypoint migration, Step 5 complete Python-free production deployment, and Step 6 full DB/restart/duplicate/fault/rollback E2E remain open. Keep Python sources as references/rollback until their later gates pass.

Paper/read-only safety remains mandatory. No signing, private keys, seed phrases, live transaction construction or broadcast. No user database has been modified. No production cutover is authorized by this update.

---

# Rust-Only Production Runtime Migration

## Acceptance target

The deployed application must run entirely from the Rust binary. Python may remain temporarily as a behavioral reference during parity work, but no production entry point, request handler, monitor worker, paper executor, or deployment command may import, invoke, or require Python.

This is a migration gate, not a declaration that migration is complete.

## Verified branch audit

Audit ref: `rewrite/rust-backend-20261109` (reviewed 2026-11-13).

| Production responsibility | Rust implementation found | Python production path still present | Status |
|---|---|---|---|
| Dashboard HTTP/API and static UI serving | Rust `dashboard_server.rs`, `serve-dashboard`, route-level API E2E and TCP HTTP tests; launcher process smoke test is now in CI | `dashboard/server.py` remains the documented production launcher | DRAFT IMPLEMENTED / CUTOVER BLOCKED |
| Dashboard-to-monitor/paper bridge | `transaction_monitor`, `paper_copy_runner`, `monitor_service` modules exist | `dashboard/monitor_bridge.py` is imported by the Python server | BLOCKED |
| Background monitor lifecycle | `monitor_service.rs` implements opt-in worker lifecycle | `dashboard/monitor_service.py` is still imported by the Python server | PARITY/INTEGRATION NOT CERTIFIED |
| Paper BUY/SELL ledger | `paper_simulator.rs` implements persistent paper orders, reservations, fills and reports | `engine/paper_simulator.py` remains imported by the Python server | PARITY/INTEGRATION NOT CERTIFIED |
| Copy-order queue and execution | `paper_copy_runner.rs` exists | `engine/paper_copy_runner.py` remains imported by the Python server | PARITY/INTEGRATION NOT CERTIFIED |
| Profiles, source wallets and transaction history | `profile_store.rs` exists and documents Python SQLite schema compatibility | `engine/profile_store.py` remains imported by the Python server | API/integration parity not certified |
| P&L / portfolio valuation | `pnl.rs` and Rust paper report valuation exist; tests cover fresh/missing/stale valuation behavior | `engine/pnl.py` remains imported by the Python server and used by Python report paths | RUST CORE TESTED / PYTHON CUTOVER BLOCKED |
| RPC, transaction processing and validation | Rust modules exist: `solana_public`, `wallet_monitor`, `transaction_processor`, `transaction_monitor`, `signature_store`, `solana_validation` | Python behavioral-reference modules and the Python dashboard path remain | Rust core and read-only RPC smoke are CI-tested; production cutover blocked |\n| Telegram paper bot | No Rust Telegram adapter with demonstrated API/ledger parity was found in this audit | `engine/telegram_bot.py` uses `engine.paper_trading.PaperTrader` | BLOCKED |\n| Root/Stage 1 health-check entry points | Rust `check-config` exists; complete `engine.health`/`run_checks` parity is not yet demonstrated | Root `main.py` and `Engen-main/Engen-main/main.py` remain Python entry points | BLOCKED |

The inspected Python dashboard imports the Python monitor bridge, monitor service, P&L, market-data, paper-copy runner, paper simulator and Solana public-RPC modules. A Rust-native HTTP server and `serve-dashboard` CLI entry point have now been added on draft branch `migration/rust-dashboard-runtime-20261113` (PR #49), wiring the Rust stores, monitor service, transaction processor and paper-copy runner. However, it has not yet been proven to preserve every legacy response/data behavior, and no successful CI/E2E result is recorded for this draft. The canonical application README and Python production files have not yet been switched or removed. Therefore the Rust runtime is not yet certified as the sole production runtime.

## Latest tested implementation evidence

- CI run [43461548653](https://github.com/santosreemangal-lgtm/Engen/actions/runs/43461548653) completed successfully with the Rust dashboard module, profile/wallet/paper API end-to-end test, 178 Rust tests, public read-only RPC smoke, Python Phase 2/Phase 8 behavioral references, dashboard syntax checks, Clippy and the migration report gate.
- That run preceded the latest monitor opt-out persistence, direct TCP HTTP test, Python production-path audit expansion and launcher-process CI test. Those newer changes require their own latest successful CI run before they are considered verified.

## Current work completed in the draft branch\n\n- Added a Rust-native HTTP server and the `engen-engine serve-dashboard` command.\n- Added route handlers for wallet CRUD, RPC health, profiles/settings/source-wallet associations, profile transaction pagination, monitor lifecycle, and paper-simulation routes.\n- Wired profile monitoring to Rust signature discovery/transaction processing, profile persistence, Fresh-lane-only paper queueing, and the Rust paper simulator. History-lane results are retained for display but excluded from paper-order creation.\n- Kept HTTP on loopback by default, with bounded request bodies, API rate limiting, optional bearer authentication, same-origin write validation, and response security headers.\n\nThese source changes have partial CI evidence: route-level API E2E, Rust core tests, legacy profile-ledger read-only compatibility tests, and basic runtime checks have passed in earlier runs. The direct TCP request test, launcher process E2E, monitor-stop ordering regression and expanded Python blocker audit are added but need the current CI run to finish. This is not evidence that every legacy API/data behavior or restart scenario is certified.\n\n## Required migration sequence

1. Implement the Rust HTTP server and serve the existing dashboard assets from Rust.
2. Preserve the current API contract: wallets, monitor status/control, RPC health, profiles/settings/source-wallet associations, profile monitor lifecycle, profile transaction pagination, and global/profile paper-simulation routes.
3. Wire each profile's Rust monitor to the Rust transaction processor, durable signature/profile stores, and paper-copy queue/executor. Preserve Fresh-first scheduling; History must never create paper orders.
4. Use the Rust paper simulator as the only runtime ledger. A read-only compatibility layer now exposes legacy `paper_balances`/`paper_trades`; tests cover a legacy schema without `quote_amount` and ensure legacy token balances with unknown marks remain visible without fabricating total portfolio value. Existing user databases have not been copied into a migration rehearsal or cut over.
5. Persist explicit monitor opt-ins and verify restart recovery, clean shutdown, one-worker-per-profile behavior, and isolation between profile ledgers.
6. A Python-free Rust launcher script now exists and CI is being extended to start it over localhost HTTP. The old Python README launcher, root health-check entry points and Telegram bot are still present; deployment/run instructions have not been switched.
7. Add a CI runtime gate proving the production startup path does not invoke Python. Python may still run temporarily in CI only for reference/parity tests; it must not be a production dependency.
8. Pass API contract, end-to-end, restart, database compatibility, duplicate-prevention, paper accounting, security and regression tests.
9. Only after all gates pass, remove Python production modules. Keep any required behavioral references under a clearly named test/reference-only location, with no import path from production Rust code.

## Non-negotiable safety invariants

- Public-wallet RPC remains read-only.
- Paper orders and fills remain virtual records only.
- No signing, private-key/seed-phrase handling, transaction construction for live execution, or transaction broadcasting.
- Fresh lane may create paper orders; History lane may only backfill/report.
- Unknown or stale prices must not be treated as zero or used to fabricate portfolio value.
- Existing SQLite/profile/signature/audit data must not be deleted or overwritten during cutover.

## How to use the audit script

Run `bash scripts/audit_rust_runtime_migration.sh` to print the current blocker report. Use `--strict` when the migration is believed complete; strict mode exits non-zero while a known Python production entry point or dependency remains.

A green Rust unit-test run alone is not sufficient evidence of a Rust-only production runtime.
