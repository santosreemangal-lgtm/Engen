# Phase 1 — Canonical Rust Migration Audit

Audit date: 2026-11-10
Repository: santosreemangal-lgtm/Engen
Canonical PR: #48
Canonical branch: rewrite/rust-backend-20261109
Audited head: c7cd35815c2cb4519e282f8179d9041e76e89f73
Baseline: feature/engen-vision-core-20261108

## 1. Canonical decision
PR #48 is the sole canonical review surface for all Python-to-Rust backend/runtime migration work. All future migration commits belong on rewrite/rust-backend-20261109 and are reviewed through PR #48. PR #47 is superseded and must receive no further migration work. This designation does not authorize merging: #48 remains Draft until all acceptance gates pass.

## 2. Audit scope and evidence
Inspected PR metadata and changed-file list, all 19 Rust source modules, Cargo.toml, MIGRATION_STATUS.md, rust-engine CI workflow, baseline repository tree, and representative Python counterparts for configuration, RPC policy/client, Solana validation, freshness and signature persistence. The Actions run attached to audited head completed successfully: cargo test --all-targets and cargo clippy --all-targets -- -D warnings passed; the workflow's formatting-normalization step also passed. This was not a runtime launch, live-provider test, differential test suite, SQLite compatibility test against user data, coverage-instrumented run, or deployment test. No user data/ database was accessed.

## 3. Rust module parity matrix
Status definitions: FOUNDATION COMPLETE means a narrow helper exists with focused Rust unit tests and current CI success; it does not mean production parity. PARTIAL means implementation exists but full Python parity, integration, restart/fault tests or production acceptance are unproven. NOT IMPLEMENTED means no Rust equivalent exists in this PR.

| Rust module | Python counterpart | Status | Remaining acceptance work |
|---|---|---|---|
| config.rs | engine/config.py; engine/rpc_policy.py | FOUNDATION COMPLETE | Verify all environment defaults/edge cases and production wiring. |
| rpc_policy.rs | engine/rpc_policy.py | FOUNDATION COMPLETE | Differential transport tests: failures, Retry-After, pacing and retry semantics. |
| safety.rs | engine/safety.py | FOUNDATION COMPLETE | Whole-crate/API-surface proof that no live capability is reachable. |
| solana_validation.rs | engine/solana_validation.py; engine/solana_public.py | FOUNDATION COMPLETE | Cross-language fuzz/property fixtures and malformed-input parity. |
| source_freshness.rs | engine/source_freshness.py | FOUNDATION COMPLETE | Integrate pre-queue and pre-commit checks; retain stale audit state. |
| latency.rs | engine/latency.py; transaction/paper latency | PARTIAL | End-to-end source-receipt-to-committed-fill instrumentation and target verification. |
| solana_public.rs | engine/solana_public.py | PARTIAL | Full method/response/error parity and mocked transport integration tests. |
| transaction_analysis.rs | engine/transaction_analysis.py | PARTIAL | Differential classification corpus, native SOL/SPL edge cases, evidence completeness and regressions. |
| transaction_processor.rs | engine/transaction_processor.py | PARTIAL | Python parity fixtures, null blockTime pipeline handling, metadata/evidence parity. |
| transaction_monitor.rs | engine/transaction_monitor.py | PARTIAL | Retry/pending transitions, bounded processing, durable queue drain and evidence parity. |
| signature_store.rs | engine/signature_store.py | PARTIAL | Exact schema/index/default compatibility, atomic transitions, idempotency and fault recovery. |
| profile_store.rs | engine/profile_store.py | PARTIAL | Full CRUD/API parity, profile isolation, backup/restore and disposable-fixture schema tests. |
| wallet_monitor.rs | engine/wallet_monitor.py | PARTIAL | Durable cursor recovery, before-cursor paging, 1,000-signature cap and failure recovery. |
| paper_simulator.rs | engine/paper_simulator.py; engine/paper_trader.py; engine/paper_trading.py | PARTIAL | Atomic order/fill, idempotency, sell-all/proportional sells, fees, no oversell and restart. |
| paper_copy_runner.rs | engine/paper_copy_runner.py; engine/paper_trader.py; engine/paper_trading.py; engine/pending_copy_report.py | PARTIAL | Queue orchestration, pending report, event order, price/liquidity guards and E2E restart. |
| pnl.rs | engine/pnl.py | PARTIAL | Differential ledger/P&L fixtures, fees/cost-basis edge cases and persistence integration. |
| market_data.rs | engine/market_data.py | PARTIAL | Provider fallback/cache parity, price/liquidity staleness and execution integration. |
| monitor_scheduler.rs | engine/monitor_runner.py; engine/paper_monitor_runner.py | PARTIAL | Prove actual worker uses one cycle, Fresh-first, capped History, cooldown and persisted backlog. |
| monitor_service.rs | dashboard/monitor_service.py; dashboard/monitor_bridge.py | PARTIAL | Connect scheduler/scanner/processor/copy runner; lifecycle and API status compatibility. |
| lib.rs | Python package/runtime composition | PARTIAL | Library exports modules only; no production binary or assembled runtime. |

## 4. Python components without production Rust replacements

| Python component | Required Rust destination |
|---|---|
| dashboard/server.py; server_with_pending.py | Rust HTTP server, routes, middleware, static asset serving; consolidate duplicate server paths |
| dashboard/__main__.py; main.py; run_dashboard.py | Rust binary/bootstrap and explicit runtime composition |
| dashboard/monitor_bridge.py | Rust application-service wiring for profile/store/scanner/processor/copy pipeline |
| engine/telegram_bot.py | Paper-only Telegram adapter; no execution/broadcast API |
| engine/health.py; engine/logger.py | Health/readiness and structured logging |
| engine/apply_source_freshness.py | Rust maintenance/verification subcommand; no implicit user DB mutation |
| tools/collect_wallet_transactions.py | Rust operational CLI |
| tools/evaluate_transaction_labels.py | Rust fixture/evaluation CLI |
| tools/verify_profile_store_recovery.py | Rust recovery verification CLI |
| Python tests under engine/engine/engine/tests | Equivalent Rust unit/integration/API/security/E2E tests before retirement |

Retain dashboard/index.html and existing CSS/JS assets; serve them from Rust. A Rust/WASM browser rewrite is out of scope.

## 5. Test and CI audit
- Current Rust CI is green on audited head; cargo test --all-targets and cargo clippy --all-targets -- -D warnings passed.
- Workflow currently runs cargo fmt --all, which normalizes files rather than enforcing a non-mutating check. Follow-up should use cargo fmt --all -- --check.
- Green Rust tests do not prove Python parity. Full dashboard API/security, classification regression corpus, fault injection/restart and paper-copy E2E acceptance remain outstanding.
- No coverage percentage/branch coverage claim is made; no coverage report was inspected.
- No source-receipt-to-committed-fill latency measurement is available; under-100-ms remains unverified.

## 6. Target architecture and dependency direction

```text
Rust binary / bootstrap
  ├── configuration, safety policy, structured logging
  ├── HTTP API + static browser asset serving
  │     └── application services (profiles, monitoring controls, history, balances)
  ├── opt-in monitoring runtime (one scan-cycle gate)
  │     ├── Fresh-first scheduler; History only after Fresh; bounded batches
  │     ├── wallet discovery + durable cursor
  │     ├── read-only Solana RPC client
  │     ├── transaction processor + conservative classifier
  │     ├── signature/profile persistence
  │     └── paper-copy orchestration
  │           ├── source freshness recheck
  │           ├── market-data snapshot/guards
  │           ├── atomic virtual paper simulator
  │           └── exact-decimal P&L ledger
  ├── paper-only Telegram adapter
  ├── health/readiness + metrics/latency
  └── operational CLI subcommands
```

Rules: (1) domain modules do not depend on HTTP/runtime globals; (2) persistence uses explicit store interfaces and atomic transactions/idempotency keys; (3) RPC/market-data adapters are read-only with bounded timeouts, response sizes, retries and safe errors; (4) monitoring orchestrates scanner -> processor -> stores -> paper-copy service and contains no accounting rules; (5) HTTP and Telegram call shared application services and expose no live execution path; (6) only bootstrap constructs adapters and starts workers; monitoring remains off until explicit opt-in; (7) migration/build/test must never open or mutate user data/ databases—compatibility tests use disposable copied fixtures.

## 7. Production runtime and cutover sequence
1. Add Rust application composition and one binary with explicit server, worker and maintenance subcommands; prevent duplicate monitor workers.
2. Port dashboard HTTP API/security middleware and preserve browser response contracts/assets.
3. Complete store parity and test compatibility on disposable DB copies; no automatic user DB migration at startup.
4. Complete classification and monitoring parity: Fresh-first, History batches 10–25, newest 1,000 signatures per wallet, source age 60s/future tolerance 5s, one worker per cycle, HTTP 429 cooldown 30s doubling to 10 minutes and Retry-After honored.
5. Complete exact-decimal paper execution, atomic/idempotent fills, proportional sells, full close, no oversell and restart recovery.
6. Port Telegram, health/logging, deployment entrypoints and operational utilities.
7. Run Python-vs-Rust differential fixtures and full Rust unit/integration/API/security/fault-injection/restart/E2E suites.
8. Verify compatibility on copied fixtures; test production-like deployment; measure chain detection separately from source-receipt-to-committed-fill. Local processing/commit target remains under 100 ms.
9. Keep Python runtime intact until every gate passes. Cutover requires a separate reviewed step. No live trading, signing, private keys or transaction broadcast may be introduced.

## 8. Phase 1 conclusion
Canonical PR/branch: DECIDED — PR #48 only. Architecture: DECIDED — Rust production backend/runtime, existing browser UI retained. Current foundation CI: PASS. Rust modules present: 19. Production parity-complete modules proven by this audit: NONE. Production HTTP/API entrypoint: NOT IMPLEMENTED. Overall migration: IN PROGRESS; PR #48 remains Draft. No user database, Python runtime, running branch or deployment was modified.