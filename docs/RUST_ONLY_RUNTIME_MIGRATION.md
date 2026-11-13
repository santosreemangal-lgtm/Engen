# Rust-Only Production Runtime Migration

## Acceptance target

The deployed application must run entirely from the Rust binary. Python may remain temporarily as a behavioral reference during parity work, but no production entry point, request handler, monitor worker, paper executor, or deployment command may import, invoke, or require Python.

This is a migration gate, not a declaration that migration is complete.

## Verified branch audit

Audit ref: `rewrite/rust-backend-20261109` (reviewed 2026-11-13).

| Production responsibility | Rust implementation found | Python production path still present | Status |
|---|---|---|---|
| Dashboard HTTP/API and static UI serving | New `rust-engine/src/dashboard_server.rs` and `serve-dashboard` CLI command; API/E2E parity is not yet CI-certified | `dashboard/server.py` remains the existing documented production launcher | IMPLEMENTED IN DRAFT / NOT CERTIFIED |
| Dashboard-to-monitor/paper bridge | `transaction_monitor`, `paper_copy_runner`, `monitor_service` modules exist | `dashboard/monitor_bridge.py` is imported by the Python server | BLOCKED |
| Background monitor lifecycle | `monitor_service.rs` implements opt-in worker lifecycle | `dashboard/monitor_service.py` is still imported by the Python server | PARITY/INTEGRATION NOT CERTIFIED |
| Paper BUY/SELL ledger | `paper_simulator.rs` implements persistent paper orders, reservations, fills and reports | `engine/paper_simulator.py` remains imported by the Python server | PARITY/INTEGRATION NOT CERTIFIED |
| Copy-order queue and execution | `paper_copy_runner.rs` exists | `engine/paper_copy_runner.py` remains imported by the Python server | PARITY/INTEGRATION NOT CERTIFIED |
| Profiles, source wallets and transaction history | `profile_store.rs` exists and documents Python SQLite schema compatibility | `engine/profile_store.py` remains imported by the Python server | API/integration parity not certified |
| P&L / portfolio valuation | `pnl.rs` exists | `engine/pnl.py` remains imported by the Python server and used by Python report paths | PARITY/INTEGRATION NOT CERTIFIED |
| RPC, transaction processing and validation | Rust modules exist: `solana_public`, `wallet_monitor`, `transaction_processor`, `transaction_monitor`, `signature_store`, `solana_validation` | Python behavioral-reference modules and tests remain | Core parity exists in part; production routing not certified |

The inspected Python dashboard imports the Python monitor bridge, monitor service, P&L, market-data, paper-copy runner, paper simulator and Solana public-RPC modules. A Rust-native HTTP server and `serve-dashboard` CLI entry point have now been added on draft branch `migration/rust-dashboard-runtime-20261113` (PR #49), wiring the Rust stores, monitor service, transaction processor and paper-copy runner. However, it has not yet been proven to preserve every legacy response/data behavior, and no successful CI/E2E result is recorded for this draft. The canonical application README and Python production files have not yet been switched or removed. Therefore the Rust runtime is not yet certified as the sole production runtime.

## Current work completed in the draft branch\n\n- Added a Rust-native HTTP server and the `engen-engine serve-dashboard` command.\n- Added route handlers for wallet CRUD, RPC health, profiles/settings/source-wallet associations, profile transaction pagination, monitor lifecycle, and paper-simulation routes.\n- Wired profile monitoring to Rust signature discovery/transaction processing, profile persistence, Fresh-lane-only paper queueing, and the Rust paper simulator. History-lane results are retained for display but excluded from paper-order creation.\n- Kept HTTP on loopback by default, with bounded request bodies, API rate limiting, optional bearer authentication, same-origin write validation, and response security headers.\n\nThese are source changes only. They are not evidence of build success, exact API parity, legacy-ledger migration, or deployment readiness.\n\n## Required migration sequence

1. Implement the Rust HTTP server and serve the existing dashboard assets from Rust.
2. Preserve the current API contract: wallets, monitor status/control, RPC health, profiles/settings/source-wallet associations, profile monitor lifecycle, profile transaction pagination, and global/profile paper-simulation routes.
3. Wire each profile's Rust monitor to the Rust transaction processor, durable signature/profile stores, and paper-copy queue/executor. Preserve Fresh-first scheduling; History must never create paper orders.
4. Use the Rust paper simulator as the only runtime ledger. Preserve SQLite compatibility and validate existing data through read-only backups and temporary-copy migration tests before any production cutover.
5. Persist explicit monitor opt-ins and verify restart recovery, clean shutdown, one-worker-per-profile behavior, and isolation between profile ledgers.
6. Switch all production launchers, deployment definitions, scripts and user-facing run instructions to the Rust binary. Keep loopback binding, request bounds, authentication/Origin protections and rate limiting.
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
