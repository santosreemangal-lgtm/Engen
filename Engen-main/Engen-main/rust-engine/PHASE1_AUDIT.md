# Phase 1 — Current-Head Rust Architecture, Parity & Test Audit

Audit date: 2026-11-10  
Repository: santosreemangal-lgtm/Engen  
Canonical PR: [#48](https://github.com/santosreemangal-lgtm/Engen/pull/48)  
Canonical branch: \`rewrite/rust-backend-20261109\`  
Audited source head: \`da98d527f82027071ab3103580af9a32dbb0958b\`  
Baseline: \`feature/engen-vision-core-20261108\`

> Scope: source-code and repository-tree audit of the exact PR head. This is not a claim of runtime parity, deployment readiness, or production cutover approval.

## 1. Canonical PR decision

PR #48 is the **only canonical Rust migration PR**. All migration work must be stacked on \`rewrite/rust-backend-20261109\` and reviewed in PR #48. PR #47 is superseded and must not receive further migration work.

PR #48 must remain **Draft and unmerged** until all migration and acceptance gates pass. No production cutover is authorized by this audit.

## 2. Exact-head evidence and scope

At the audited head:

- 20 Rust source modules exist under \`rust-engine/src\`.
- 38 non-test, non-\`__init__.py\` Python production/runtime files remain in the repository.
- 34 Python test files remain as behavioral references.
- The Rust crate declares a library target only. \`Cargo.toml\` has no \`[[bin]]\` target; \`src/lib.rs\` declares modules and exports a small API, but does not compose or start a production application.
- The current PR-head GitHub Actions run #109 completed successfully. Its workflow runs \`cargo fmt --all\` (format normalization), \`cargo test --all-targets\`, and \`cargo clippy --all-targets -- -D warnings\`. This is a crate-level check, not an application launch or Python/Rust parity run.
- The audit counted 97 inline Rust unit tests across the 20 modules. There is no Rust integration-test directory/file in this tree, no coverage-instrumented report, and no verified coverage percentage.
- No live RPC call, user database access, data migration, or deployment was performed for this audit.

### Important audit correction

The previous version of this report named an older audited head (\`c7cd358...\`) and described 19 modules. This version supersedes it: the current PR head is \`da98d527...\` and contains 20 Rust modules. Historical findings must not be treated as current-head verification.

## 3. Rust-to-Python module parity and test matrix

Status meanings:

- **NARROW HELPER COMPLETE** — the narrow helper contract is implemented and has focused tests; this does not mean the full production component or its integration is accepted.
- **PARTIAL** — Rust implementation exists, but Python behavioral parity, complete integration, failure/restart coverage, or production acceptance remains unproven.
- **INCOMPLETE** — the module is only a declaration/export shell or does not implement the required runtime composition.

| Rust module | Python counterpart(s) | Inline Rust tests | Current status | Main remaining work |
|---|---|---:|---|---|
| \`config.rs\` | \`engine/config.py\` | 5 | PARTIAL | Compare every environment variable/default/serialization behavior; prove config is loaded once by production bootstrap. |
| \`latency.rs\` | \`engine/latency.py\` | 3 | PARTIAL | Python API parity; instrument real source-receipt-to-committed-fill path; bounded concurrency and end-to-end target proof. |
| \`lib.rs\` | Python package/runtime composition | 0 | INCOMPLETE | No app composition, server, worker lifecycle, binary entrypoint, or operational subcommands. |
| \`market_data.rs\` | \`engine/market_data.py\` | 4 | PARTIAL | Provider fallback/cache semantics, response/error parity, staleness/liquidity guards, mocked transport tests and copy-runner integration. |
| \`monitor_scheduler.rs\` | \`engine/monitor_runner.py\`, \`engine/paper_monitor_runner.py\` | 5 | PARTIAL | Scheduler policy exists, but prove it is used by the actual monitor service; durable queue drain, restart and cooldown integration. |
| \`monitor_service.rs\` | \`dashboard/monitor_service.py\`, \`dashboard/monitor_bridge.py\` | 3 | PARTIAL | Current service accepts a generic scan callback; it is not wired to wallet discovery → transaction processing → persistence → paper-copy. Port status/API semantics and durable monitor state. |
| \`paper_copy_runner.rs\` | \`engine/paper_copy_runner.py\`, \`engine/paper_trader.py\`, \`engine/paper_trading.py\`, \`engine/pending_copy_report.py\` | 8 | PARTIAL | Full queue/status parity, persisted pending work, all sizing/settings cases, market-data integration, event ordering, restart/fault E2E. |
| \`paper_simulator.rs\` | \`engine/paper_simulator.py\` | 6 | PARTIAL | Initial-balance and ledger compatibility, all order states/rejections/fees, atomicity/idempotency and broader restart/fault parity. |
| \`pnl.rs\` | \`engine/pnl.py\` | 4 | PARTIAL | Differential ledger fixtures for every transaction type, fees/cost basis/unknown basis and persistence/report parity. |
| \`profile_store.rs\` | \`engine/profile_store.py\` | 7 | PARTIAL | Full schema/index/default/API compatibility, migrations on disposable copies, route-level profile isolation, failure/backup recovery parity. |
| \`rpc_policy.rs\` | \`engine/rpc_policy.py\` | 4 | PARTIAL | Python transport behavior includes request pacing and HTTP/transport/provider error distinctions; compare retries, Retry-After and safe error mapping end-to-end. |
| \`safety.rs\` | \`engine/safety.py\` | 1 | PARTIAL | Python emergency-stop latch and all authorization guard semantics are not shown as equivalent; prove no live capability is reachable across the complete binary/API surface. |
| \`signature_store.rs\` | \`engine/signature_store.py\` | 5 | PARTIAL | Exact schema/index/default parity, all pending/review transitions, atomic writes, pruning/retention and fault-injection recovery. |
| \`solana_public.rs\` | \`engine/solana_public.py\` | 5 | PARTIAL | Python client method/error/transport parity, timeout and retry behavior, confirmation/finality handling, mocked RPC integration. |
| \`solana_validation.rs\` | \`engine/solana_validation.py\` | 3 | NARROW HELPER COMPLETE | The public-key/signature Base58 length contract is implemented; add shared Python/Rust fixture corpus before declaring cross-language parity. |
| \`source_freshness.rs\` | \`engine/source_freshness.py\` | 4 | NARROW HELPER COMPLETE | The 60-second age and 5-second future-skew boundary helper is implemented; prove both pre-queue and pre-commit integration and stale audit retention. |
| \`transaction_analysis.rs\` | \`engine/transaction_analysis.py\` | 9 | PARTIAL | Differential classification corpus and all legacy cases; native SOL/SPL, Pump.fun/inner instruction, ambiguous owner and UNKNOWN/pending behavior. |
| \`transaction_monitor.rs\` | \`engine/transaction_monitor.py\` | 6 | PARTIAL | Complete pending/retry/confirmation transitions, bounded processing, durable queue drain and monitor-to-paper-copy integration. |
| \`transaction_processor.rs\` | \`engine/transaction_processor.py\` | 6 | PARTIAL | Null blockTime and incomplete metadata parity, provider error handling, fixture parity and downstream evidence persistence. |
| \`wallet_monitor.rs\` | \`engine/wallet_monitor.py\` | 6 | PARTIAL | Python paging/baseline/cursor semantics, newest-1,000 history cap, persisted pagination recovery and RPC-failure parity. |

**Classification result:** 2 modules are complete only within their narrow helper contracts; 17 modules are partial; \`lib.rs\` is incomplete. **Zero modules are proven complete as end-to-end production-parity components.**

## 4. Remaining Python production/runtime inventory

These 18 files have no dedicated Rust production equivalent in the current PR:

| Python file | Required Rust destination |
|---|---|
| \`dashboard/__main__.py\` | Rust binary/bootstrap command |
| \`dashboard/server.py\` | Rust HTTP server, routes, middleware and static asset serving |
| \`dashboard/server_with_pending.py\` | Consolidated Rust API behavior; remove duplicate server implementation only after parity |
| \`engine/apply_source_freshness.py\` | Explicit Rust maintenance/verification subcommand; no implicit user-DB mutation |
| \`engine/health.py\` | Rust health/readiness checks |
| \`engine/logger.py\` | Rust structured logging and secret redaction |
| \`engine/paper_monitor_runner.py\` | Rust monitor lifecycle and paper-copy worker composition |
| \`engine/paper_trader.py\` | Rust paper execution service/API parity |
| \`engine/paper_trading.py\` | Rust paper trading orchestration and settings parity |
| \`engine/pending_copy_report.py\` | Rust pending-copy status/report API |
| \`engine/telegram_bot.py\` | Rust Telegram paper-only adapter; no live execution/broadcast surface |
| \`Engen-main/Engen-main/main.py\` | Rust operational CLI/bootstrap |
| \`Engen-main/Engen-main/run_dashboard.py\` | Rust server entrypoint |
| \`tools/collect_wallet_transactions.py\` | Rust read-only collection CLI |
| \`tools/evaluate_transaction_labels.py\` | Rust offline fixture/evaluation CLI |
| \`tools/verify_profile_store_recovery.py\` | Rust disposable-fixture recovery verifier |
| repository-root \`main.py\` | Consolidate/replace legacy root entrypoint |
| repository-root \`run_dashboard.py\` | Consolidate/replace legacy root entrypoint |

Three additional Python files have only partial Rust analogues and still require production wiring/parity: \`dashboard/monitor_bridge.py\`, \`dashboard/monitor_service.py\`, and \`engine/monitor_runner.py\`. Their existence must not be counted as completed migration merely because Rust modules with similar names exist.

The existing browser assets (HTML/CSS/JavaScript) may remain and should be served by Rust. They are not part of the Python backend replacement count.

## 5. Test coverage audit

### Rust test inventory

There are 97 inline Rust unit tests in total:

| Rust module | Tests |
|---|---:|
| config | 5 |
| latency | 3 |
| lib | 0 |
| market_data | 4 |
| monitor_scheduler | 5 |
| monitor_service | 3 |
| paper_copy_runner | 8 |
| paper_simulator | 6 |
| pnl | 4 |
| profile_store | 7 |
| rpc_policy | 4 |
| safety | 1 |
| signature_store | 5 |
| solana_public | 5 |
| solana_validation | 3 |
| source_freshness | 4 |
| transaction_analysis | 9 |
| transaction_monitor | 6 |
| transaction_processor | 6 |
| wallet_monitor | 6 |
| **Total** | **97** |

### Python behavioral reference inventory

The repository contains 34 Python test files. They cover dashboard profile API/security, latency, monitor baseline/persistence/service, paper-copy restart/runner/latency, simulator initial balance, paper trader/trading, pending-copy report/retry, P&L, profile/signature stores, RPC/safety/Solana validation/freshness, Telegram, transaction analysis/classification/latency/monitor/processor/RPC and wallet monitoring.

Rust unit tests exercise useful cases, but the repository does not yet demonstrate one-to-one porting of all 34 Python test files. In particular, dashboard API/security, Telegram, operational CLI, deployment, full monitor-to-paper-copy E2E, DB compatibility and broad restart/fault-injection tests are not proven by the current Rust suite. No line/branch coverage percentage is claimed because no coverage report was generated or inspected.

### CI interpretation and required correction

Run #109 is green for the existing library crate. It does **not** prove production server startup, full Python behavioral parity, integration wiring, DB compatibility, or the under-100-ms paper-fill target.

The workflow currently executes \`cargo fmt --all\`, which formats files rather than enforcing a non-mutating check. Change this to \`cargo fmt --all -- --check\` in a dedicated CI-hardening slice, then add explicit Rust integration/API/security/fault/restart/E2E test targets.

## 6. Current Rust dependency graph

The imports in the current Rust modules show this dependency direction:

\`\`\`text
config ───────────────> rpc_policy
solana_public ────────> rpc_policy + solana_validation
market_data ─────────> pnl + solana_validation
profile_store ───────> solana_validation
signature_store ─────> solana_validation
wallet_monitor ──────> signature_store + solana_public + solana_validation + latency
transaction_processor > solana_public + solana_validation + transaction_analysis + latency
transaction_monitor ─> wallet_monitor + transaction_processor + transaction_analysis
                       + signature_store + solana_public + solana_validation
                       + source_freshness + latency
paper_simulator ─────> pnl + safety
paper_copy_runner ───> paper_simulator + market_data + pnl + source_freshness
                       + transaction_analysis + latency

monitor_scheduler     (standalone; no current consumer in monitor_service)
monitor_service ─────> solana_validation + caller-supplied scan callback
latency, pnl, transaction_analysis, safety, rpc_policy,
solana_validation, source_freshness (foundation/domain leaves)
\`\`\`

This is a reasonable lower-level module direction, but it is not yet a production application architecture: \`monitor_service\` is not composed with \`monitor_scheduler\`, \`wallet_monitor\`, \`transaction_monitor\`, persistence and \`paper_copy_runner\`. The scan callback is injected by an external caller, and no Rust HTTP server or bootstrap currently provides that caller.

## 7. Final target production architecture

The approved target is one Rust production binary with explicit subcommands and a single shared application-service layer:

\`\`\`text
Rust binary / bootstrap
  ├── Config + fail-closed paper-only safety + redacted logging
  ├── Shared application services
  │    ├── Profile / settings / balances / history
  │    ├── Wallet scan / transaction analysis / status
  │    └── Paper-copy queue / simulator / P&L
  ├── HTTP server + API security + existing static HTML/CSS/JS
  ├── Opt-in monitoring runtime (one cycle gate)
  │    ├── Fresh-first scheduler, then bounded History
  │    ├── Wallet discovery + durable cursor/signature store
  │    ├── Read-only Solana RPC adapter
  │    ├── Transaction processor + conservative classifier
  │    ├── Profile/signature persistence
  │    └── Paper-copy orchestration
  │         ├── freshness recheck before commit
  │         ├── market-data snapshot and guards
  │         ├── atomic/idempotent virtual simulator
  │         └── exact-decimal P&L ledger
  ├── Paper-only Telegram adapter
  ├── Health/readiness + metrics/latency
  └── CLI: collect / evaluate / verify-recovery / maintenance
\`\`\`

Dependency rules:

1. Domain and accounting modules must not depend on HTTP handlers, Telegram or process globals.
2. Store interfaces own SQLite transactions, idempotency and schema compatibility; application services coordinate stores.
3. RPC and market-data adapters are read-only, bounded by timeouts/response size, and expose typed/sanitized errors.
4. The monitoring orchestrator calls scheduler → wallet scanner → transaction processor/classifier → stores → paper-copy service. It does not implement accounting itself.
5. HTTP and Telegram call the same application services; neither has a separate business-logic implementation.
6. Only bootstrap constructs adapters and starts workers. Monitoring remains disabled until explicit opt-in, and only one cycle/worker is allowed.
7. Existing browser assets remain unchanged unless separately approved; Rust serves them.
8. Build/test/migration must never access or mutate the user's real \`data/\` databases. Use disposable copied fixtures only.

## 8. Non-negotiable runtime behavior

- Paper/read-only only: no live mode, signing, private keys/seeds, transaction construction for live execution, or broadcast API.
- Source freshness: block time must exist, be no more than 60 seconds old, and no more than 5 seconds in the future; recheck before paper-fill commit.
- Fresh-first, then capped History; one worker per cycle.
- History limited to newest 1,000 signatures per wallet; old backlog is never copied.
- History batch 10–25.
- HTTP 429 cooldown starts at 30 seconds, doubles to 10 minutes, honors Retry-After, and resets after success.
- Preserve durable pending/review/audit state; no destructive pruning of paper orders, fills or source trade references.
- Paper fills must be atomic/idempotent, proportional sells must not oversell, 100% source sells must close, and restart recovery must be proven.
- Measure chain detection separately from source-receipt-to-committed-paper-fill; local processing/commit target remains under 100 ms.

## 9. Ordered Phase 1 exit criteria

1. Freeze PR #48 as the only canonical migration surface; keep Draft/unmerged.
2. Accept this current-head inventory and architecture as the baseline; correct any module/file ownership discrepancies before implementation.
3. Complete shared Python/Rust golden fixtures for config, RPC, validation, freshness, transaction classification, persistence, paper fills and P&L.
4. Complete narrow parity modules first, then store/processing/monitoring/paper-copy integration.
5. Implement the Rust binary/bootstrap, HTTP/API/security and static asset serving; wire the real monitoring pipeline.
6. Port the 18 unimplemented Python production/runtime files and the three partially represented orchestration files.
7. Port all relevant 34 Python behavioral test files into Rust unit/integration/API/security/fault/restart/E2E coverage; do not weaken or skip tests.
8. Test schema compatibility only on disposable copied DB fixtures. Do not open or modify real user \`data/\` databases.
9. Add deployment/health/rollback verification and prove a clean Rust-only production runtime with no Python sidecar.
10. Measure the under-100-ms local paper-fill processing/commit target separately from chain detection.
11. Run a separate cutover review only after all gates pass. Keep Python reference/rollback code until then.

## 10. Phase 1 conclusion

- Canonical PR: **DECIDED — PR #48 only**.
- Canonical branch: **DECIDED — \`rewrite/rust-backend-20261109\`**.
- Current module inventory: **20 Rust modules**.
- Narrow helper contracts complete: **2** (\`solana_validation\`, \`source_freshness\`).
- Partial Rust modules: **17**.
- Incomplete module: **1** (\`lib.rs\` as application composition/runtime).
- Rust inline unit tests: **97**.
- Python behavioral reference test files: **34**.
- Production Python runtime files with no dedicated Rust equivalent: **18**, plus 3 partially represented orchestration files.
- Production Rust binary/server/runtime: **NOT IMPLEMENTED**.
- End-to-end Python/Rust parity and coverage percentage: **NOT PROVEN**.
- Current Rust crate CI run #109: **PASS**, limited to the current library crate checks.
- Overall migration: **IN PROGRESS**. PR #48 remains Draft and unmerged.

No user database, active runtime, running checkout, deployment or live-trading capability was modified by this audit.
