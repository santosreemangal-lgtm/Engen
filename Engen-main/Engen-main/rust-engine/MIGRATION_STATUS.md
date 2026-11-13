# Rust Backend Migration — Status

**Target baseline:** `feature/engen-vision-core-20261108`  
**Canonical migration branch:** `rewrite/rust-backend-20261109`  
**Review PR:** #48 (Draft; do not merge until every gate passes)

## Final architecture decision

All production application/runtime components are to be implemented in Rust: backend APIs, monitoring, wallet scanning, transaction interpretation, persistence, paper execution, market-data access, Telegram bot, workers, command-line utilities, and deployment entrypoints. Python is a temporary compatibility/reference implementation only and is not the target production runtime.

The existing browser UI remains HTML/CSS/JavaScript and is served by the Rust web server. Rewriting the browser client in Rust/WASM is not required for a Rust-only backend/runtime.

Python tests are temporary behavioral references. Every required test case must be represented in Rust unit/integration/E2E tests before Python runtime modules or test fixtures are retired.

## Safety and data invariants

- Public Solana RPC reads only; monitoring remains opt-in.
- Paper balances, orders, fills, and P&L are virtual only.
- No private-key or seed-phrase ingestion, storage, or logging.
- No signing, live transaction construction, or transaction-broadcast API.
- No live-mode switch or hidden execution path.
- Freshness: source block time must be present, no more than 60 seconds old, and no more than 5 seconds in the future.
- Preserve exact raw token amounts and decimals; use decimal-safe accounting.
- Fresh-first scheduling, then capped History; one worker per cycle.
- History is limited to the newest 1,000 signatures per wallet. Old backlog is never copied.
- On HTTP 429, start with a 30-second cooldown, exponential backoff to 10 minutes, honor Retry-After, and reset after success.
- Never read, rewrite, migrate, prune, or delete user `data/` databases as a side effect of source-code migration.

## Rust modules present on this branch

The branch currently contains Rust implementations for:

- `config.rs`, `safety.rs`: strict configuration and fail-closed no-live-execution policy.
- `source_freshness.rs`, `solana_validation.rs`, `rpc_policy.rs`: source freshness, public key/signature validation, and RPC endpoint policy.
- `solana_public.rs`: read-only JSON-RPC client boundary.
- `signature_store.rs`: SQLite signature/deduplication/retry state foundation.
- `paper_simulator.rs`, `pnl.rs`: persistent paper orders/fills and decimal-safe accounting foundation.
- `profile_store.rs`: profile CRUD, profile settings, source-wallet associations, transaction history, exact balance snapshots/aggregation, soft deletion, and SQLite backup.
- `latency.rs`: bounded local-stage latency samples and percentile summaries.

These modules are not automatically considered parity-complete just because they exist or compile. Their APIs and persistence behavior must pass the acceptance checks below.

## Remaining production ports

1. **Transaction intelligence:** port `transaction_analysis.py`, `transaction_processor.py`, classification regressions, evidence preservation, explicit UNKNOWN outcomes, native SOL/SPL balance changes, and parsed/inner instruction context.
2. **Wallet scanning:** port `wallet_monitor.py` and `transaction_monitor.py`, including baseline behavior, before-cursor pagination, durable anchors, deduplication, retry state, pending recovery, and bounded history.
3. **Automatic paper-copy orchestration:** port `paper_copy_runner.py`, `paper_trader.py`, `paper_trading.py`, and `pending_copy_report.py`; include idempotent queueing, proportional sells, full-close behavior, no overselling, price/liquidity guards, and restart recovery.
4. **Market data:** port `market_data.py` and its bounded HTTP/public-provider parsing, exact numeric validation, and timestamp/liquidity freshness.
5. **Monitoring runtime:** port `dashboard/monitor_bridge.py`, `dashboard/monitor_service.py`, `monitor_runner.py`, and `paper_monitor_runner.py`; preserve one-worker-per-cycle, Fresh-first/History-capped scheduling, cooldown, and stop/start lifecycle.
6. **Dashboard API/server:** port `dashboard/server.py`, `dashboard/server_with_pending.py`, `dashboard/__main__.py`, `main.py`, and `run_dashboard.py`; retain the existing browser assets and prove route/response compatibility, security checks, body limits, origin checks, and profile isolation.
7. **Telegram:** port `telegram_bot.py` to a paper-only Rust bot with no execution or broadcast capability.
8. **Utilities/operations:** port `health.py`, `logger.py`, `apply_source_freshness.py`, and the `tools/` wallet collection, label evaluation, and profile recovery verification commands to Rust binaries/subcommands.
9. **Deployment/docs:** update Cargo workspace/build commands, Render process entrypoints, Codespaces setup, environment-variable documentation, health checks, and rollback procedures for the Rust runtime.
10. **Test migration:** port the behavior in all relevant Python tests into Rust unit/integration tests, including fault injection, data compatibility, restart recovery, dashboard API/security, Telegram, and end-to-end paper-copy flow.

## Required acceptance gates

- Rust formatting, `cargo test --all-targets`, and `cargo clippy --all-targets -- -D warnings` pass on the canonical branch.
- Transaction classification parity is demonstrated against recorded safe fixtures; ambiguous or incomplete cases remain UNKNOWN/pending and never become trade candidates.
- Signature/profile database schemas and current user data remain readable without an automatic migration or destructive operation.
- Paper orders/fills are idempotent and atomic; proportional sells cannot oversell; restart recovery is proven.
- Monitoring obeys the freshness, batch, history, worker, and 429 cooldown rules above.
- Dashboard API and UI workflows are end-to-end tested on Rust.
- Source-receipt-to-committed-paper-fill latency is measured separately from chain-to-detection; target is under 100 ms for the local processing/commit stage.
- No real trades, signing, private keys, or transaction broadcasts are possible.

## Cutover rule

Do not delete or disable the Python runtime until every required Rust port is implemented, behaviorally verified, data-compatible, restart-safe, and deployable. Keep PR #48 Draft until all acceptance gates are green. Do not merge, switch the user's running branch, touch live user databases, or introduce live trading as part of this migration.
