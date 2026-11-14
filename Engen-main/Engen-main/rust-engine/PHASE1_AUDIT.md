# Superseding audit update — 2026-11-14 (PR #49 draft branch)

This addendum supersedes any conflicting implementation-status statements in the historical 2026-11-10 audit below. The historical audit is retained for traceability; its source head and CI links are not evidence for the current branch.

**Current review branch:** `migration/rust-dashboard-runtime-20261113`  
**Current PR:** [#49](https://github.com/santosreemangal-lgtm/Engen/pull/49), Draft, unmerged  
**Base:** `rewrite/rust-backend-20261109`  
**Current audited scope:** Rust component ports and their known Python counterparts. This is not the final production-cutover audit.

## Updated Step 1 result

| Python responsibility | Rust implementation on PR #49 | Current judgement |
|---|---|---|
| Dashboard server, static assets, API handler foundation | `dashboard_server.rs`, `serve-dashboard`, existing browser assets served by Rust | PORTED CORE; exact full API-contract parity is Step 2 and remains unaccepted |
| Dashboard monitor bridge / lifecycle | `dashboard_server.rs` ScanContext + `monitor_service.rs` + `monitor_scheduler.rs` + `wallet_monitor.rs` | PORTED CORE; whole-runner recovery and complete lifecycle parity still need tests |
| Configuration and execution safety | `config.rs`, `safety.rs` | PORTED CORE; compare every legacy setting and deployment path |
| Health checks | `health.rs`, `health-check` CLI | PORTED CORE; deployment readiness/health integration remains |
| Logging and secret redaction | `logging.rs`, `setup_logging` in Rust CLI | PORTED CORE; latest logger changes passed CI on the preceding commit, newer copy-risk changes are under CI |
| Latency recording | `latency.rs` | PORTED CORE; actual detection-to-paper-commit benchmark not yet evidenced |
| Market price lookup | `market_data.rs` | PORTED CORE; differential provider/fallback coverage remains |
| Monitor scheduling and backoff | `monitor_scheduler.rs` | PORTED CORE; wired into the final runtime path and durable operational recovery still need full proof |
| Paper copy queue / pending report | `paper_copy_runner.rs`, `dashboard_server.rs` pending report | PORTED CORE; differential settings/order-state coverage remains |
| Legacy profile risk controls | `paper_copy_runner.rs` now parses and enforces `max_position_pct` and `max_open_positions`; includes pending BUY reservations in open-position counting | NEW PORT; tests added, latest CI still required |
| Paper ledger and P&L | `paper_simulator.rs`, `pnl.rs` | PORTED CORE; full legacy schema, retention, data migration and restart compatibility remain separate acceptance gates |
| Profile and signature persistence | `profile_store.rs`, `signature_store.rs` | PORTED CORE; whole-application compatibility rehearsal on disposable DB copies remains |
| RPC policy and public Solana reads | `rpc_policy.rs`, `solana_public.rs` | PORTED CORE; keep provider edge cases and operational load/retry parity under regression testing |
| Address/signature validation and source freshness | `solana_validation.rs`, `source_freshness.rs`, checks wired into transaction and paper-copy flow | PORTED CORE; full shared golden-fixture parity remains |
| Transaction analysis, processor and monitor | `transaction_analysis.rs`, `transaction_processor.rs`, `transaction_monitor.rs` | PORTED CORE; complete differential corpus, rare instruction/Token-2022 cases and provider failure matrix remain |
| Wallet scanning and pagination | `wallet_monitor.rs`, `signature_store.rs` | PORTED CORE; restart/fault and live-provider rate-limit acceptance remains |
| Standalone Telegram paper bot | `telegram.rs`, `serve-telegram-paper-bot` | PORTED; intentionally separate in-memory paper ledger, not shared with dashboard simulator |
| Standalone operational monitor CLI | Dashboard monitor service plus Rust one-shot scan command | CORE SCAN/LIFECYCLE PORTED; exact CLI argument/output parity for the old Python runners is not yet accepted |
| Wallet sample collection / CSV label evaluation / profile recovery utility scripts | Rust scanner and profile-store backup tests cover parts of their responsibilities | TOOLING PARITY INCOMPLETE; exact standalone collection/evaluation/recovery commands remain migration work |

## Latest verification evidence

- CI run [#584](https://github.com/santosreemangal-lgtm/Engen/actions/runs/43567479482), commit `e26af5a9d4ca488428eef8c6eb88ca3684d99df5`: **all workflow steps passed** — rustfmt, Rust tests, read-only RPC smoke, selected Python behavior references, dashboard checks, Rust launcher process E2E, no-Python release runtime check, Clippy and static migration audit.
- The legacy profile risk settings `max_position_pct` and `max_open_positions` were added after that green commit. The latest risk-control commit is `97f851695c2a4811f39a8bff73dce5eaa899e77f`; its latest workflow is still the authoritative pending verification target. Earlier CI failures on intermediate risk commits were rustfmt-only and were corrected.
- A green run on `e26af5a` does **not** certify the subsequently added risk-limit changes.

## Step 1 acceptance decision

**Step 1 is not yet closed.** The primary Rust domain components and dashboard runtime counterparts now exist, and the known profile risk-control omission has been ported with focused tests. However, full Python/Rust differential acceptance across all production responsibilities has not yet been proven. In particular:

1. The new `max_position_pct` / `max_open_positions` port must pass the latest Rust unit tests and Clippy.
2. The complete transaction-classification and transaction-processing corpus still needs a recorded differential result, including malformed/missing fields, native SOL, SPL Token and Token-2022, inner instructions, versioned lookups and explicit UNKNOWN outcomes.
3. The complete simulator/copy-runner matrix still needs differential coverage for all profile settings, pending reservations, order rejection reasons, proportional SELL, partial/full-close, fees, stale price/liquidity, and persistence/restart.
4. Utility-tool behavior (wallet-sample artifact generation, reviewed-label evaluation, and standalone recovery verification) is not yet exposed through equivalent Rust operational subcommands. These must be migrated or explicitly retired with documented replacement commands.
5. The older Python files remain in the tree. They are not removed or disabled until the parity tests and later runtime/deployment gates pass.

The current Rust code must remain paper-only and read-only. No real user database has been opened, rewritten, migrated, or pruned. PR #49 remains Draft and unmerged.

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