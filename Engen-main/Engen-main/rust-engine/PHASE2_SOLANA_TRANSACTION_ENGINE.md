# Phase 2 — Solana Transaction Engine: Current-Head Implementation Audit

Audit date: 2026-11-10  
Canonical PR: #48 — sole Rust migration PR  
Branch: \`rewrite/rust-backend-20261109\`  
Audited head before Phase 2 preservation fix: \`18bb159705ff92901ae75a94c967a0c3bc9cb304\`  
Phase 2 fix commits are recorded in the PR history; re-audit the latest branch head before acceptance.

> This is an implementation audit and gap register, not a claim that Phase 2 is complete. All RPC validation and transaction fixtures must remain offline/mocked. No user data database may be opened or changed.

## 1. Executive status

**Phase 2: IN PROGRESS — NOT ACCEPTED.**

The current Rust code has a substantial read-only Solana RPC client, Base58 length validation, transaction metadata normalization, native SOL and SPL token balance-delta extraction, conservative swap-candidate heuristics, retryable transaction processing, a monitor pipeline and a persistent signature/evidence store.

However, this is not yet a complete production-grade transaction engine. Important gaps remain in explicit transaction-type classification (especially \`TRANSFER\`), full parity against Python behavior, versioned transaction edge cases, malformed/incomplete balance-account alignment, RPC transport hardening, and integration/fault/E2E coverage.

## 2. Component-by-component result

| Requested capability | Current Rust implementation | Status | Remaining acceptance work |
|---|---|---|---|
| Solana public RPC client | Read-only method allowlist; JSON-RPC envelope/id validation; timeout; bounded retry count; HTTP 429/5xx backoff; Retry-After parsing; health/slot/blockhash/status/signature-page/transaction calls | PARTIAL | Add bounded response-body size; classify/retry provider-level rate-limit errors where appropriate; validate all response shapes and transaction-version support; compare all Python timeout/error/transport semantics; mock-server tests for every branch and concurrency. |
| Wallet address validation | Base58 decoding and exact 32-byte public-key length; used by monitor and signature store | NARROW HELPER COMPLETE | Shared Python/Rust golden vectors, edge-case corpus and all entrypoint validation tests. |
| Transaction signature validation | Base58 decoding and exact 64-byte signature length; RPC, monitor and store use it | NARROW HELPER COMPLETE | Shared Python/Rust golden vectors, malformed Unicode/whitespace/leading-zero cases and integration validation. |
| Transaction parsing | Normalizes slot, status, fee, signer set, program IDs, outer instructions and inner instructions | PARTIAL | Preserve and test all required instruction/account/index/data evidence, address lookup table handling, transaction versions, absent/invalid metadata and Python output compatibility. |
| BUY / SELL classification | Conservative wallet-owned balance-delta heuristic; stablecoin/native SOL quote handling; Pump.fun V2 instruction context; emits BUY_CANDIDATE / SELL_CANDIDATE | PARTIAL | Complete shared labelled corpus; broaden known DEX/aggregator cases; distinguish candidate from proven execution; confidence calibration and false-positive tests. |
| TRANSFER classification | Transfer instructions are retained as evidence and transfer-only balance movement is not promoted to a swap candidate; no explicit \`TRANSFER\` transaction-type result is currently exposed | INCOMPLETE | Add explicit, separately exposed \`transaction_type=TRANSFER\` for verified transfer cases while keeping trade_signal non-copyable; distinguish outer transfer from inner swap settlement transfers. |
| UNKNOWN and other types | Conservative UNKNOWN fallback; failed metadata can be identified as FAILED_TRANSACTION | PARTIAL | Define explicit output contract for FAILED, UNKNOWN and safely supported OTHER transaction types; never infer OTHER from incomplete evidence. |
| Native SOL balance changes | Uses pre/post lamports, loaded addresses and fee-payer fee adjustment; now rejects account-key/pre/post-balance length mismatch and malformed account/balance entries rather than silently truncating | PARTIAL — CI PENDING | Cover lookup-table addresses, rent/account lifecycle, fee payer/non-fee payer, failed tx and incomplete arrays with shared fixtures. |
| SPL token balance changes | Uses pre/post token balances, raw integer amount strings, decimals, owner conflict handling and per-account/mint deltas | PARTIAL | Cover missing token accounts, owner changes, duplicate/malformed rows, account-index mismatch, decimals mismatch, token-2022 and all Python parity cases. |
| Instruction evidence | Normalized outer/inner instruction context in analysis; raw transaction JSON is stored in transaction_records | PARTIAL | Prove exact evidence survives every success/failure/retry/restart path; test size limits and evidence immutability/retention. |
| Failed / ambiguous / incomplete transactions | Failed chain metadata is excluded from BUY/SELL; missing metadata remains retryable; ambiguous signer/confirmation/owner cases stay UNKNOWN; raw payload may be retained | PARTIAL | Preserve failed/incomplete status end-to-end; add explicit failed/incomplete/finality/fault matrix and no-candidate assertions. |
| Transaction processor | Signature validation, RPC fetch, metadata validation, balance analysis and normalized output | PARTIAL | Full Python differential parity; null blockTime behavior; provider/metadata failure classification; versioned transactions; bounded batch processing; integration with durable monitor/store. |
| Transaction monitor | Fetches and processes signatures; checks signer and confirmation; persists transaction evidence; retries incomplete/errors; marks stale candidates | PARTIAL | End-to-end wallet scan → monitor → store proof; duplicate batch handling; concurrency/race tests; all confirmation transitions; no lost pending work on restart. |
| Duplicate signature detection | SQLite primary key \`(wallet_address, signature)\`, INSERT OR IGNORE, idempotent transaction-record upsert, duplicate-save tests | PARTIAL | Prove repeated signatures are not fetched/processed twice in one batch or across concurrent workers; test cross-wallet independence and restart recovery under fault injection. |

## 3. Important defect found and corrected in this Phase 2 work

A failed transaction was saved with \`transaction_records.processing_status='failed'\`, but the subsequent \`mark_processed\` call overwrote that evidence status with \`processed\`. The raw JSON remained, but the persisted classification was misleading.

Fix applied to both the Rust and Python SignatureStore reference implementations:
- \`mark_processed\` no longer overwrites transaction-record statuses \`stale\`, \`failed\`, \`incomplete\`, or \`needs_review\`.
- Added a Rust unit test proving failed status and raw transaction evidence survive \`mark_processed\`.
- Added a Python regression test for the same behavior.

This is a correctness fix, not proof that the whole Phase 2 pipeline is complete. The new GitHub Actions result must pass before accepting it.

## 4. Current Rust test evidence

Previous audited module-level Rust test inventory (new transaction-type and alignment regression tests have since been added; rerun CI to establish the current count):

| Module | Tests |
|---|---:|
| \`solana_public.rs\` | 5 |
| \`solana_validation.rs\` | 3 |
| \`transaction_analysis.rs\` | 9 |
| \`transaction_processor.rs\` | 6 |
| \`transaction_monitor.rs\` | 6 |
| \`signature_store.rs\` | 6 (includes the new failed-status regression test) |

These are focused unit tests, not complete branch coverage. No coverage percentage is claimed. Python tests remain the behavioral reference and need differential execution against matching Rust fixtures.

## 5. Required architecture for accepted Phase 2

\`\`\`text
Read-only SolanaRpcClient
  ├── getSignaturesForAddress (bounded page + validated cursor)
  ├── getSignatureStatuses (confirmation/finality/error)
  └── getTransaction (bounded response + version-aware parsing)
           |
           v
Wallet/signature validation
           |
           v
TransactionProcessor
  ├── validate slot, blockTime, metadata and fee
  ├── parse message/account keys/lookup-table addresses
  ├── preserve outer + inner instruction evidence
  ├── calculate exact native SOL + SPL balance deltas
  └── preserve incomplete/failed state without fabrication
           |
           v
Transaction classification
  ├── BUY / SELL candidate (never inferred from balance delta alone)
  ├── TRANSFER (explicit non-trade type)
  ├── FAILED
  ├── UNKNOWN (ambiguous/incomplete)
  └── OTHER only when positively supported
           |
           v
TransactionMonitor
  ├── signer ownership + confirmed/finalized checks
  ├── duplicate/idempotency guard
  ├── bounded retry / needs_review
  └── durable raw evidence + processing status
           |
           v
SignatureStore (SQLite, disposable fixtures in tests)
\`\`\`

## 6. Mandatory Phase 2 acceptance tests

1. RPC mock server: success, malformed JSON, wrong JSON-RPC id/version, provider error, HTTP 429 with Retry-After seconds/date, 5xx, timeout, connection failure, oversized body, and unsupported transaction version.
2. Validation corpus: valid/invalid Base58 addresses and signatures, exact decoded lengths, whitespace and malformed encodings; identical expected results in Python and Rust.
3. Transaction parser corpus: legacy and supported versioned transactions, static and lookup-table account keys, outer/inner instructions, absent metadata, null blockTime, invalid fee/slot and failed metadata.
4. Classification corpus: native SOL buy/sell, SPL/stablecoin buy/sell, direct SOL/SPL transfers, swap settlement inner transfers, Pump.fun V2 buy/sell, account creation/close/rent, fee-only, multi-asset, ambiguous owner, conflicting quotes and unknown programs.
5. Balance corpus: exact integer lamports/token amounts, fee payer adjustment, account creation/closure, missing/extra balance rows, loaded addresses, decimals mismatch and failed transactions.
6. Evidence persistence: raw payload, parsed outer/inner evidence, failure reason and status survive retry, process restart and duplicate delivery.
7. Duplicate protection: duplicate in one batch, duplicate across scans, duplicate across concurrent worker attempts, restart, and same signature observed for two different wallets.
8. E2E offline test: wallet signature discovery → RPC fixture → processor → classifier → evidence store; no real RPC, no user database, no live trading/broadcast.
9. Differential tests: same fixtures and expected outputs in Python and Rust, including UNKNOWN/FAILED/TRANSFER semantics.
10. GitHub Actions: cargo fmt check, cargo test, clippy, Rust integration tests and Python reference tests; report results from the latest commit only.

## 7. Safety and operational constraints

- Only public, read-only Solana RPC methods are allowed.
- No private keys, seed phrases, signing, transaction construction for live execution, or broadcast methods.
- No automatic paper-copy action may be triggered by TRANSFER, FAILED, UNKNOWN, OTHER, incomplete or ambiguous activity.
- Never use the real \`data/\` directory or the user's live databases for migration tests. Use disposable temporary SQLite fixtures only.
- Keep PR #48 Draft and unmerged. Phase 2 completion does not itself authorize deployment or cutover.

## 8. Phase 2 exit decision

- RPC client: **PARTIAL**
- Address/signature validators: **NARROW HELPER COMPLETE**
- Parser/instruction evidence: **PARTIAL**
- BUY/SELL: **PARTIAL**
- Explicit TRANSFER output: **NOT IMPLEMENTED**
- Native SOL/SPL balance analysis: **PARTIAL** (native account/balance alignment hardening added; broader fixtures pending)
- Failed/ambiguous/incomplete handling: **PARTIAL** (failed-status overwrite regression fixed; CI pending)
- Processor: **PARTIAL**
- Monitor: **PARTIAL**
- Persistent duplicate storage guard: **IMPLEMENTED**, full concurrent/in-batch proof pending
- Full Phase 2 acceptance: **NOT PASSED**

Do not mark Phase 2 complete until all mandatory acceptance tests pass on the latest PR #48 head.


## 9. 2026-11-10 implementation update — latest work, not yet accepted

The following additional changes have been committed on the canonical branch after the earlier gap table above. Their implementation exists in the branch, but the full new CI and acceptance matrix must pass before Phase 2 can be accepted.

| Gap previously recorded | New implementation | Verification still required |
|---|---|---|
| Unbounded RPC response bodies | Read-only response bodies are capped at 8 MiB, including unknown/chunked content-length streams. Oversized bodies return a typed `ResponseTooLarge` error. | Run bounded-body tests and verify monitor persistence/review behavior. |
| Provider-level rate limits | JSON-RPC provider errors with rate-limit codes/messages are retried within bounded exponential backoff; Retry-After continues to be honoured for HTTP responses. | Confirm mock-server retry and terminal-error cases in latest CI. |
| Unsupported transaction versions | Provider unsupported-version errors are typed and moved directly to `needs_review`, rather than repeated blindly on each scan. The client explicitly requests support through transaction version 0. | Test unsupported-version mapping against multiple provider error envelopes. Version 1+ decoding is not claimed. |
| Transfer classification | Explicit `transaction_type` is now separate from `trade_signal`: BUY, SELL, TRANSFER, FAILED, OTHER, UNKNOWN. Direct transfer type requires a trusted System/SPL Token/SPL Token-2022 program ID; transfer and failed/other/unknown types stay `NOT_COPYABLE`. | Confirm differential golden corpus and E2E test. |
| Token-2022 | Direct Token-2022 `transferChecked` evidence is covered as a non-copyable TRANSFER. | Broader Token-2022 account/balance extension fixtures remain required. |
| Versioned lookup-table instructions | Outer and inner compiled-instruction program indices are resolved using static plus loaded writable/readonly addresses, without duplicating keys already expanded by parsed RPC responses. | Confirm the shared v0 lookup-table fixture in both languages. This is not support for future message versions beyond 0. |
| Native SOL alignment | Python now follows Rust's strict key/pre/post balance length and value checks; malformed or partial account-index arrays yield no inferred deltas. | Run Python reference tests and Rust tests. |
| Duplicate work | Monitor skips duplicate signatures in the same in-memory batch and terminal persisted statuses; SQLite tests cover repeated batch inserts, concurrent duplicate inserts, cross-wallet independence, and restart evidence. | The current worker model still needs a full race/fault test for concurrent monitor execution, not just storage insert contention. |
| Evidence persistence | A disposable-fixture offline end-to-end test walks baseline → signature discovery → transaction RPC mock → parser/classifier → SQLite raw outer/inner evidence → close/reopen → evidence recovery. | Run the new Rust integration test. Additional retry/failure mutation tests remain. |
| Python/Rust differential | A common JSON golden corpus is now consumed by Rust integration tests and Python behavioral-reference tests for lookup-table transfer, Token-2022 transfer, failed transaction, explicit-swap BUY, account lifecycle OTHER and unknown-program TRANSFER spoofing. | Latest CI must prove both runners execute and pass against the same file. Expand corpus across native SOL/SPL edge cases. |
| CI correctness | CI now uses non-mutating `cargo fmt --all -- --check`, Rust all-target tests, Python Phase 2 reference tests, and Clippy. Python source/test changes are in the workflow path filter. | Initial strict-format run #169 failed because numerous existing Rust files are not rustfmt-clean; its test steps were skipped. The exact formatted workspace output is being generated as a temporary artifact so formatting can be normalized in a follow-up commit; a green check is not yet available. |

### New offline test assets

- `rust-engine/tests/transaction_pipeline.rs`: local loopback mock-RPC end-to-end and shared golden-corpus Rust tests.
- `rust-engine/tests/fixtures/phase2_transactions.json`: one source of truth used by both Rust and Python tests.
- `engine/engine/engine/tests/test_transaction_analysis.py`: Python shared-corpus, versioned lookup-table and strict balance-alignment regressions.

### Updated Phase 2 exit status

**IN PROGRESS — NOT ACCEPTED.** The new tests and implementations do not yet have a green latest-head CI result. A formatter-only failure is not a test failure, but it blocks acceptance because downstream jobs were skipped. Do not mark any item accepted solely because the older Run #127 was green.
