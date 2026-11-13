# Phase 2 — Solana Transaction Engine: Current-Head Implementation & Acceptance Report

Audit date: 2026-11-10  
Audited source head: \`06bceb284d57d29ff5f27d9ffd8c47dd8616ee01\`  
Canonical branch: \`rewrite/rust-backend-20261109\`  
Canonical review: PR #48 (Draft; do not merge before every phase gate passes)

> All tests in this report are offline or use loopback mock RPC servers and disposable SQLite fixtures. No production RPC endpoint, user \`data/\` database or live-trading capability was used.

## 1. Executive status

**Implementation is substantial and latest-head CI is green. Phase 2 is not yet accepted as a complete production-parity transaction engine.**

### Latest CI evidence

PR check run #247: https://github.com/santosreemangal-lgtm/Engen/actions/runs/42876209174  
Push verification run #246: https://github.com/santosreemangal-lgtm/Engen/actions/runs/42876194051

| Check | Result |
|---|---:|
| Rust library unit tests | 117 passed |
| Rust CLI unit tests | 3 passed |
| Rust integration tests | 8 passed |
| Total Rust tests | **128 passed, 0 failed** |
| Python transaction reference tests | 102 passed |
| Python Solana validation tests | 5 passed |
| Python RPC policy tests | 10 passed |
| Python SignatureStore tests | 17 passed |
| Python wallet monitor tests | 11 passed |
| Total selected Python reference tests | **145 passed, 0 failed** |
| \`cargo fmt --all -- --check\` | PASS |
| \`cargo clippy --all-targets -- -D warnings\` | PASS |

This is a passing targeted Phase 2 suite, not a full run of all Python tests or an application E2E.

## 2. Current implementation

### Read-only RPC client

Rust \`solana_public.rs\` currently provides:
- Explicit allowlisted read-only JSON-RPC methods; there is no send/broadcast method.
- JSON-RPC 2.0 envelope and request-ID validation.
- Bounded timeout/retry count and request pacing.
- Bounded retry for HTTP 429/5xx and recognized provider-side rate-limit errors.
- Retry-After parsing for seconds and valid HTTP-date values (the date case is asserted using a generated future HTTP date), capped backoff.
- An 8 MiB maximum response body, enforced even when Content-Length is absent.
- Typed invalid-response, transport, oversized-body and unsupported-transaction-version errors.
- Signature-page row validation and bounded page sizes.
- \`getTransaction\` requests parsed JSON and advertises \`maxSupportedTransactionVersion: 0\`.

Future/unsupported transaction versions are not guessed or silently discarded: they are retained as \`needs_review\`. Version support above 0 is not claimed.

### Parsing and evidence

- Slot, fee, signer, program IDs, outer instructions and inner instructions are normalized.
- Version-0 lookup-table program indices resolve using static and loaded writable/readonly addresses.
- Raw transaction, parsed evidence and classification are persisted separately.
- Missing or invalid metadata is not fabricated.
- Failed/incomplete/unsupported evidence is retained; the later \`mark_processed\` update must not overwrite terminal evidence statuses.

### Classification

The classifier exposes separate \`transaction_type\` and \`trade_signal\` outputs:
- \`BUY\`, \`SELL\`, \`TRANSFER\`, \`FAILED\`, \`OTHER\`, \`UNKNOWN\`.
- Verified direct System/SPL Token/SPL Token-2022 transfers are explicitly non-copyable.
- Inner settlement transfers alone do not transform a swap into a direct-transfer transaction.
- Failed, OTHER, UNKNOWN, incomplete and ambiguous transactions cannot become paper-copy signals.
- Conservative BUY/SELL candidate recognition covers native SOL and SPL/stablecoin quote movements, selected Jupiter/Pump.fun V2 contexts, owner/signer checks, failed status and conflicting balance directions.

This is not a universal decoder for every DEX or aggregator program.

### Balance analysis

- Exact integer token raw units and lamports are preserved.
- Token owner conflicts and decimals mismatches are not attributed.
- Native SOL key/pre/post balance arrays must align; malformed or incomplete rows do not produce inferred deltas.
- Fee adjustment is applied to the fee payer only.
- Loaded address and outer/inner instruction contexts are preserved where supported.

### Signature/evidence storage

- Primary key is wallet + signature; repeated and cross-wallet cases are separately tested.
- Repeated values inside one batch are deduplicated.
- Concurrent SQLite inserts commit exactly one row.
- Concurrent `MonitorService.start()` calls on one service instance reuse its existing worker; an instrumented test proves no overlapping scans.
- Baseline, pending, processed, stale and needs-review states are retained.
- Raw transaction, parsed evidence and classification survive close/reopen in tested scenarios.

## 3. Shared cross-language fixtures now in the repository

Two common JSON fixtures are consumed by both Rust and Python tests.

1. \`tests/fixtures/phase2_transactions.json\` — 11 transaction scenarios covering v0 lookup-table transfer, Token-2022 transfer, failed transaction, explicit swap BUY, account lifecycle OTHER, unknown-program transfer spoofing, Jupiter SELL, native SOL BUY/SELL and contradictory deltas.
2. \`tests/fixtures/solana_validation.json\` — shared valid/invalid address and signature corpus, including malformed Base58, whitespace and non-ASCII cases.

The Phase 2 golden test asserts both \`transaction_type\` and \`trade_signal\` in both languages. The shared fixture does not yet compare every field of the complete normalized result object; full object-by-object parity is still open.

## 4. Acceptance matrix

| Acceptance area | Evidence on current head | Status |
|---|---|---|
| RPC success and JSON-RPC validation | Success, wrong ID/version, provider error and missing result cases | PASS |
| RPC malformed/oversized body | Malformed JSON plus 8 MiB bound with and without Content-Length | PASS |
| HTTP/provider retry behavior | HTTP 429 with Retry-After, provider-level rate limit, HTTP 503 retry, bounded retry count | PASS |
| RPC timeout/connection failure | Offline timeout and connection-failure regression tests | PASS |
| Retry-After seconds/date parsing | Both formats tested; cap is enforced | PASS |
| Unsupported transaction version | Typed error and durable \`needs_review\` after restart | PASS for detection/retention; versions above 0 are not decoded |
| Address/signature validation | Shared Python/Rust golden corpus and integration use | PASS for the published corpus |
| Legacy/v0 parsing and lookup-table evidence | v0 lookup resolution and outer/inner instruction tests | PARTIAL — full provider/parser edge matrix remains |
| BUY/SELL/TRANSFER/FAILED/OTHER/UNKNOWN | Unit tests and 11 shared transaction scenarios | PARTIAL — not an exhaustive DEX/aggregator corpus |
| Native SOL and SPL balance matrix | Integer precision, fee payer, owner, decimals and length-mismatch regressions | PARTIAL — complete balance-account lifecycle matrix remains |
| Evidence persistence and restart | Successful transfer, failed transaction and unsupported version scenarios survive reopen | PARTIAL — broader transient failure/retry fault matrix remains |
| Duplicate handling | Same-batch, repeated scans/terminal states, concurrent SQLite inserts, cross-wallet independence and restart tests | PARTIAL — same-instance concurrent starts are covered; independent service/process claims and lease recovery are not proven |
| Offline wallet-to-store E2E | Wallet discovery → mock RPC → processor/classifier → evidence store → restart | PASS for transaction/evidence pipeline; it does not run the whole application or paper-copy service |
| Python/Rust differential | Shared fixtures consumed in both languages and expected type/signal checked | PARTIAL — complete output/schema parity across all reference tests is not proven |
| Current-head CI | Rust tests, Python selected references, format check and Clippy | PASS |

## 5. Remaining Phase 2 work before acceptance

1. **Concurrent monitor execution:** storage inserts are safe under concurrent attempts, and concurrent `start()` calls inside one `MonitorService` instance share one worker with a max-active-scan test. The missing proof is across separate service instances/processes and after worker/process failure. Add a per-profile singleton owner or durable atomic claim/lease, then test cross-instance duplicate execution and crash/restart lease recovery.
2. **Complete differential parity:** expand the shared fixtures to compare complete normalized processor/classifier outputs, not only transaction type and trade signal. Include null blockTime, incomplete transaction, metadata errors and all important Python reference edge cases.
3. **Token-2022 and balance matrix:** extend fixtures for Token-2022 account/balance variants, missing/closed accounts, malformed/duplicate rows, owner changes, decimals mismatch and loaded-address alignment.
4. **Retry/fault recovery:** exercise pending → retry → recovered/needs_review through monitor/store restart and verify raw/parsed evidence is immutable and retained.
5. **Final current-head audit:** after those changes, rerun Rust, Python reference, differential and integration checks on the exact latest PR #48 commit.

## 6. Safety and operational constraints

- Public read-only RPC only.
- No seed/private-key handling, signing, live transaction construction or broadcast.
- No automatic paper-copy on TRANSFER, FAILED, OTHER, UNKNOWN, incomplete or ambiguous activity.
- Paper-copy logic remains a separate phase/runtime integration gate; this transaction-engine E2E does not enable it.
- Never open or mutate the user's real \`data/\` databases during tests. Use temporary disposable SQLite files.
- Keep PR #48 Draft and unmerged. Phase 2 completion does not authorize deployment or cutover.

## 7. Phase 2 conclusion

- RPC transport hardening and offline tests: **PASS**
- Shared address/signature validation corpus: **PASS**
- Transaction type distinction and current classification fixtures: **IMPLEMENTED; BROADER PARITY PARTIAL**
- Raw/parsed/classification evidence storage: **IMPLEMENTED; FULL RECOVERY MATRIX PARTIAL**
- Duplicate persistence: **IMPLEMENTED; SAME-INSTANCE WORKER SERIALIZATION TESTED; CROSS-INSTANCE/PROCESS CLAIM NOT PROVEN**
- Rust/Python shared golden execution: **PASS; COMPLETE OUTPUT PARITY NOT PROVEN**
- Current targeted CI: **PASS**
- Overall Phase 2: **IN PROGRESS — NOT ACCEPTED**

No user database, production RPC endpoint, active runtime or deployment was touched.