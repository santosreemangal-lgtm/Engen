# Step 1 — Rust component parity acceptance (2026-11-14)

**Status: COMPLETE — core application/domain component port and verification.**  
**Review branch:** `migration/rust-dashboard-runtime-20261113`  
**PR:** [#49](https://github.com/santosreemangal-lgtm/Engen/pull/49) — Draft, unmerged  
**Verified code commit:** `1f38d3373b8e193bb1f279733bf7ef6a5c2815b3`  
**CI:** [Push run #640](https://github.com/santosreemangal-lgtm/Engen/actions/runs/43571357136) and [PR run #641](https://github.com/santosreemangal-lgtm/Engen/actions/runs/43571360458) — both completed successfully.

This acceptance covers the Rust equivalents of the engine's production application/domain components. It does not certify the whole migration, API contract, application cutover, deployment, or removal of Python. Operational wrappers and standalone utility commands remain part of the later entrypoint/tool migration phase.

## Implemented Rust component map

| Production component responsibility | Rust implementation | Step 1 result |
|---|---|---|
| Configuration and paper-only execution safety | `config.rs`, `safety.rs` | Ported; legacy empty-value defaults and fail-closed behavior tested |
| Secret-safe logger | `logging.rs` | Ported; redaction, level filtering, idempotent setup, timestamp and logger identity tested |
| Health checks | `health.rs` | Rust-native health report and CLI; core required module inventory checked |
| RPC policy and public Solana reads | `rpc_policy.rs`, `solana_public.rs` | Rust components and read-only mainnet RPC smoke test pass |
| Address/signature validation | `solana_validation.rs` | Rust component and shared fixtures/tests present |
| Source freshness | `source_freshness.rs` | Configurable 60-second default via `ENGEN_MAX_SOURCE_AGE_SECONDS`; malformed limits fail closed; 5-second future tolerance retained |
| Signature persistence and duplicate tracking | `signature_store.rs` | Rust SQLite store component and tests |
| Profile persistence and profile-specific simulator path | `profile_store.rs`, `dashboard_server.rs` | Rust profile store; `PROFILE_DATA_DIR` override honored |
| Transaction analysis/classification | `transaction_analysis.rs` | Rust parser/classifier; shared Phase 2 golden corpus checked against Python |
| Transaction processing and monitoring | `transaction_processor.rs`, `transaction_monitor.rs` | Rust equivalents and tests |
| Wallet monitoring/pagination | `wallet_monitor.rs` | Rust component and read-only scan tests |
| Fresh/History scheduling and monitor lifecycle | `monitor_scheduler.rs`, `monitor_service.rs` | Rust scheduler/service components; full production cutover remains later |
| Price discovery | `market_data.rs` | Rust price snapshot validation, including zero vs negative liquidity parity |
| Paper copy queue and pending-copy reporting | `paper_copy_runner.rs`, Rust Dashboard response | Rust queue/executor and pending-copy status |
| Price-verified virtual orders, reservations, fills and reporting | `paper_simulator.rs`, `pnl.rs` | Rust simulator/P&L components |
| Legacy raw-result paper ledger API | `paper_trader.rs` | Rust compatibility port with SQLite `paper_meta`, `paper_balances`, `paper_trades`; BUY/SELL, duplicate detection, risk limits and restart tests |
| Local processing latency recorder | `latency.rs` | Python-compatible bounded sample behavior and synchronous `measure()` API; actual sub-100-ms production benchmark remains later acceptance |
| Telegram paper-only adapter | `telegram.rs` | Rust adapter exists; intentionally separate in-memory ledger, not shared with Dashboard simulator |

The legacy Python engine modules are retained as behavioral references and rollback material. They have not been deleted, renamed, or disabled.

## Verification completed on the verified commit

Both current-head workflow runs passed every step:

1. Rust formatting.
2. Rust unit and integration test suite, including the new persistent `PaperTrader` compatibility tests.
3. Public Solana mainnet read-only RPC smoke test.
4. Python Phase 2 behavioral-reference tests.
5. Python Phase 8 P&L/ROI reference tests.
6. Dashboard backend and browser script syntax.
7. Rust-native launcher smoke test confirming no Python invocation.
8. Rust Dashboard process E2E over localhost.
9. Release binary dependency check with Python runtime environment unset; no Python runtime dependency detected.
10. Clippy with warnings denied.
11. Static Python-production-path migration audit.

## Explicit boundaries — not part of Step 1 acceptance

- Exact complete Dashboard HTTP API contract parity is Step 2.
- Fully composing monitor, transaction processor, stores, market data, paper queue and simulator into the final service lifecycle, plus disposable database compatibility/restart rehearsal, is Step 3 and Step 6.
- Migrating all root/nested launchers, monitor CLI wrappers, Telegram hosting configuration, wallet collection, label evaluation and profile recovery utilities is Step 4.
- Removing Python from the production deployment/entrypoint surface and proving the entire deployed runtime is Rust-only is Step 5.
- Full fault/restart/database/duplicate prevention E2E and deployment rollback acceptance is Step 6.

The static migration audit still lists **38 Python production/runtime paths** as blockers. This is expected at this stage; no Python production source has been removed. The audit workflow being green means the inventory/report ran correctly, not that those Python files are already migrated.

## Step 1 decision

**Step 1 is closed for the core application/domain component port.** Its Rust equivalents are present and the current component/CI acceptance checks are green. The six-phase Rust-only migration remains in progress; Steps 2–6 are separate gates and are not accepted by this result.

The repository remains paper-only/read-only. No signing, private-key/seed handling, live transaction construction or broadcast was added. No real user database was opened, modified, migrated or pruned. PR #49 remains Draft and unmerged.

---

# Phase 1 — Current-Head Rust Architecture, Parity & Runtime Audit

Audit date: 2026-11-10  
Audited source head: \`06bceb284d57d29ff5f27d9ffd8c47dd8616ee01\`  
Canonical branch: \`rewrite/rust-backend-20261109\`  
Canonical review: PR #48 (Draft; do not merge before all gates pass)  
Baseline: \`feature/engen-vision-core-20261108\`

> This report distinguishes an inventory/audit deliverable from implementation acceptance. A green crate CI run is not production runtime parity.

## 1. Current result

| Area | Current evidence | Decision |
|---|---|---|
| Rust library modules | 20 modules under \`rust-engine/src\` | Implemented, mostly partial |
| Rust executable | \`src/main.rs\` with CLI/help, config, validation, classification and one-shot read-only scan commands; real-binary smoke tests cover help/version, validation, secret-safe config output and explicit DB requirement | CLI foundation present; not a production app bootstrap |
| Rust HTTP/API server | No Rust HTTP server, API router, auth middleware or static-asset server | NOT IMPLEMENTED |
| Production runtime composition | No single bootstrap composes API, profile/paper services, monitoring scheduler, wallet scan, transaction processor, stores and paper-copy runner | NOT IMPLEMENTED |
| Rust tests on audited head | 117 library unit tests + 3 CLI unit tests + 8 integration tests = 128 passed | CI PASS |
| Python Phase 2 reference tests | 145 selected tests across transaction, validation, RPC, signature-store and wallet-monitor suites | CI PASS |
| Python source inventory | 76 Python files: 35 test files, 3 package \`__init__.py\`, 38 non-test/non-init production/runtime files | Inventory verified |
| Full Python/Rust behavioral parity | No complete route/store/runtime differential or full-suite parity report | NOT PROVEN |
| Coverage | No coverage-instrumented report generated | No percentage claimed |
| Real data or production cutover | Not touched | No migration/cutover authorized |

CI evidence for this audited head:
- Rust Engine Checks PR run #247: https://github.com/santosreemangal-lgtm/Engen/actions/runs/42876209174
- Push verification run #246: https://github.com/santosreemangal-lgtm/Engen/actions/runs/42876194051
- Formatting, \`cargo test --all-targets\`, selected Python references and Clippy passed.

## 2. Canonical repository decision

PR #48 is the only canonical Rust migration PR. PR #47 is superseded. All further migration work must stay on \`rewrite/rust-backend-20261109\` and PR #48.

PR #48 must remain Draft and unmerged until the full migration, compatibility, runtime, fault/restart, security and deployment gates pass. This audit does not authorize a production cutover.

## 3. Rust module parity

Status definitions:
- **NARROW HELPER COMPLETE** — the limited helper contract has focused tests; this is not whole-component acceptance.
- **PARTIAL** — code exists, but full Python parity, application wiring, operational behavior or failure/restart proof is incomplete.
- **INCOMPLETE** — the module does not compose or start the production runtime.

| Rust module | Python counterpart | Current status | Remaining acceptance work |
|---|---|---|---|
| \`config.rs\` | \`engine/config.py\` | PARTIAL | Compare every environment variable, defaults, validation and redacted serialization; load once through production bootstrap. |
| \`latency.rs\` | \`engine/latency.py\` | PARTIAL | Instrument the actual source-receipt-to-paper-ledger-commit path; prove bounded concurrent behavior and target on a representative run. |
| \`lib.rs\` | Package/runtime composition | INCOMPLETE | Exports modules but does not construct services, HTTP server, worker lifecycle or production runtime. |
| \`market_data.rs\` | \`engine/market_data.py\` | PARTIAL | Provider/cache/fallback parity, stale-price/liquidity guards, mock transport and copy-runner integration. |
| \`monitor_scheduler.rs\` | \`engine/monitor_runner.py\`, \`engine/paper_monitor_runner.py\` | PARTIAL | Fresh-first/capped-history policy exists; it is not yet the driver of the production monitor service. Durable queue/restart/cooldown integration remains. |
| \`monitor_service.rs\` | \`dashboard/monitor_service.py\`, \`dashboard/monitor_bridge.py\` | PARTIAL | Lifecycle worker accepts a generic callback; it is not wired to wallet discovery, processing, evidence stores and paper-copy as one service. |
| \`paper_copy_runner.rs\` | \`engine/paper_copy_runner.py\`, \`engine/paper_trader.py\`, \`engine/paper_trading.py\`, \`engine/pending_copy_report.py\` | PARTIAL | Route/API parity, all queue/settings cases, durable pending work and restart/fault E2E. |
| \`paper_simulator.rs\` | \`engine/paper_simulator.py\` | PARTIAL | Full legacy ledger/schema compatibility, order-state/rejection matrix, fees, atomicity, retention and recovery parity. |
| \`pnl.rs\` | \`engine/pnl.py\` | PARTIAL | Differential fixtures for all transaction types, cost basis, fees, unknown basis and reporting. |
| \`profile_store.rs\` | \`engine/profile_store.py\` | PARTIAL | Full schema/index/default/API parity, migrations on disposable copies, route-level isolation and backup recovery. |
| \`rpc_policy.rs\` | \`engine/rpc_policy.py\` | PARTIAL | End-to-end pacing/retry/error parity and sanitized failures. |
| \`safety.rs\` | \`engine/safety.py\` | PARTIAL | Prove every service, API, CLI and adapter is fail-closed and has no reachable live capability. |
| \`signature_store.rs\` | \`engine/signature_store.py\` | PARTIAL | Complete schema/index parity, queue/claim semantics, atomic recovery, retention and fault injection. |
| \`solana_public.rs\` | \`engine/solana_public.py\` | PARTIAL | Mocked RPC coverage is substantially expanded; remaining provider edge cases, pacing/concurrency and complete Python transport parity. |
| \`solana_validation.rs\` | \`engine/solana_validation.py\` | NARROW HELPER COMPLETE | Shared Python/Rust golden fixture now covers valid/invalid public addresses and signatures. Entrypoint and full integration parity still need proof. |
| \`source_freshness.rs\` | \`engine/source_freshness.py\` | NARROW HELPER COMPLETE | 60-second source age and 5-second future-skew helper has boundary tests; production pre-queue and pre-commit wiring must be proven in the final runtime. |
| \`transaction_analysis.rs\` | \`engine/transaction_analysis.py\` | PARTIAL | Shared corpus and substantial regressions exist; full classification differential, Token-2022 edge cases and DEX/aggregator coverage remain. |
| \`transaction_monitor.rs\` | \`engine/transaction_monitor.py\` | PARTIAL | Discovery-to-evidence integration exists; full queue claim/recovery, worker race, finality and paper-copy wiring remain. |
| \`transaction_processor.rs\` | \`engine/transaction_processor.py\` | PARTIAL | Full null-time/incomplete/provider-error parity and shared parser corpus remain. |
| \`wallet_monitor.rs\` | \`engine/wallet_monitor.py\` | PARTIAL | Paging/cursor/baseline code exists; persisted Fresh/History lane integration and complete retry/restart parity remain. |

**Classification:** 2 narrow helper contracts complete; 17 modules partial; application composition incomplete. No full production-parity module is accepted end to end.

## 4. Python production/runtime files still requiring a Rust production equivalent

The current source tree contains 38 non-test, non-\`__init__.py\` Python production/runtime files. The following 18 do not yet have a dedicated Rust production equivalent:

| Python source | Required Rust destination |
|---|---|
| \`dashboard/__main__.py\` | Rust server/bootstrap entrypoint |
| \`dashboard/server.py\` | Rust HTTP server, API routes, middleware and static asset serving |
| \`dashboard/server_with_pending.py\` | Consolidated Rust API semantics; retain Python until parity |
| \`engine/apply_source_freshness.py\` | Explicit Rust maintenance/verification subcommand |
| \`engine/health.py\` | Rust health/readiness |
| \`engine/logger.py\` | Structured logging and secret redaction |
| \`engine/paper_monitor_runner.py\` | Rust monitor lifecycle and paper-copy composition |
| \`engine/paper_trader.py\` | Rust paper execution service/API parity |
| \`engine/paper_trading.py\` | Rust paper trading orchestration/settings parity |
| \`engine/pending_copy_report.py\` | Rust pending-copy status/report API |
| \`engine/telegram_bot.py\` | Rust Telegram paper-only adapter |
| \`Engen-main/Engen-main/main.py\` | Consolidated Rust operational CLI |
| \`Engen-main/Engen-main/run_dashboard.py\` | Rust server entrypoint |
| \`tools/collect_wallet_transactions.py\` | Rust read-only collection CLI |
| \`tools/evaluate_transaction_labels.py\` | Rust offline fixture/evaluation CLI |
| \`tools/verify_profile_store_recovery.py\` | Rust disposable-fixture recovery verifier |
| repository-root \`main.py\` | Consolidated Rust operational CLI |
| repository-root \`run_dashboard.py\` | Consolidated Rust server entrypoint |

Three more Python orchestration files have only partial Rust analogues and must not be counted as migrated merely because similarly named modules exist:

- \`dashboard/monitor_bridge.py\`
- \`dashboard/monitor_service.py\`
- \`engine/monitor_runner.py\`

The other Python runtime modules have related Rust domain modules, but their full production parity is still unproven. Existing browser HTML/CSS/JavaScript may remain; Rust must serve them.

## 5. Runtime dependency and wiring gaps

The current Rust dependency direction is suitable for a domain core, but not yet a complete application:

- \`monitor_scheduler.rs\` is not the orchestrator for \`monitor_service.rs\`.
- \`monitor_service.rs\` accepts a generic caller-supplied scan function; no production bootstrap supplies the complete wallet → RPC → processor → evidence store → paper-copy pipeline.
- \`paper_copy_runner.rs\`, \`paper_simulator.rs\`, \`market_data.rs\` and \`pnl.rs\` exist, but no one production application-service layer owns their lifecycle and shares it between HTTP, CLI and Telegram.
- No Rust HTTP server exposes the current dashboard API routes or serves the existing browser assets.
- No Rust API authentication/rate-limit/CSRF/CORS/security-header parity has been proven.
- No Rust production health/readiness and shutdown/rollback flow is composed.
- The CLI is read-only and requires an explicit SQLite path for its one-shot scan; it does not provide the complete operator/deployment interface.

## 6. Required target architecture

\`\`\`text
Rust binary / bootstrap
  ├── Config + fail-closed paper-only policy + redacted logging
  ├── Shared application-service layer
  │    ├── Profiles / settings / virtual balances / history
  │    ├── Wallet discovery / transaction evidence / monitoring status
  │    └── Paper-copy queue / simulator / P&L
  ├── Rust HTTP server + API security + existing static browser assets
  ├── Single monitoring runtime
  │    ├── Fresh-first scheduler, bounded History, one cycle gate
  │    ├── Durable cursor/signature store
  │    ├── Read-only RPC client
  │    ├── Parser / classifier / evidence persistence
  │    └── Paper-copy + market-data guard + atomic virtual ledger
  ├── Paper-only Telegram adapter
  ├── Health/readiness + structured metrics/latency
  └── Explicit CLI: scan / collect / evaluate / verify-recovery / maintenance
\`\`\`

The HTTP server and Telegram adapter must call the same application services; neither may contain a second business-logic implementation. Only bootstrap constructs adapters and starts workers. The production runtime must not need a Python sidecar.

## 7. Non-negotiable runtime contracts

- Read-only public Solana sources and virtual paper execution only.
- No live mode, signing, private-key/seed handling, live order construction or transaction broadcast.
- Source block time must exist, be at most 60 seconds old and no more than 5 seconds in the future; freshness is checked again before paper-fill commit.
- Fresh queue before capped History; history limited to the newest 1,000 signatures per wallet; old backlog is never copied.
- History batch 10–25; one worker/cycle.
- 429 cooldown starts at 30 seconds, doubles to 10 minutes, honors Retry-After and resets after success.
- Preserve audit, stale, pending/review, orders, fills and source-trade references; only explicitly approved raw payload retention may be pruned.
- Paper fills must be exact-decimal, atomic and idempotent; proportional sells cannot oversell and a full source sell must close the virtual position.
- Measure source detection separately from local source-receipt-to-paper-ledger-commit; the local target is below 100 ms. No result currently proves this target.

## 8. Ordered Phase 1 exit criteria

1. Freeze PR #48 as the sole canonical migration surface; keep Draft/unmerged.
2. Keep this audit current as code changes; do not treat a passing crate CI as app acceptance.
3. Complete shared cross-language fixtures for config, RPC, validation, freshness, transaction classification, stores, fills and P&L.
4. Finish domain/store parity with schema, error, retry, idempotency and recovery tests.
5. Build the Rust production bootstrap and compose all shared application services.
6. Implement Rust HTTP/API, authentication/security middleware and existing static browser asset serving.
7. Wire the single Fresh-first monitor runtime through wallet discovery, RPC, processing, evidence persistence and paper-copy; implement durable queue recovery.
8. Port the remaining 18 Python runtime files and the three partial orchestration components; port all relevant behavior tests without weakening/skipping.
9. Prove API security, Telegram paper-only controls, health/readiness, graceful shutdown and restart/rollback on disposable fixtures.
10. Benchmark the local paper-fill commit path separately from chain detection; prove the under-100-ms target on a stated test setup.
11. Run an isolated compatibility review on disposable DB copies, then a separate cutover review. Keep Python reference/rollback code until approval.

## 9. Phase 1 conclusion

- Phase 1 audit deliverable (current-head inventory, parity matrix, runtime architecture and ordered exit criteria): **COMPLETE**
- Rust CLI foundation: **PRESENT, PARTIAL**
- Rust HTTP/API server: **NOT IMPLEMENTED**
- Full Rust production runtime composition: **NOT IMPLEMENTED**
- Full Python/Rust parity: **NOT PROVEN**
- Full application restart/fault/deployment proof: **NOT PROVEN**
- Under-100-ms local paper-fill target: **NOT BENCHMARKED**
- Full Rust production-runtime replacement tracked by this audit: **IN PROGRESS — NOT ACCEPTED**

No user \`data/\` database was opened, migrated or modified. No production process or deployment was changed.