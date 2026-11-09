# Engen full Rust migration

Status: migration branch created; initial Rust foundation only. This is **not** a completed port.

## Baseline and safety

- Baseline: `feature/engen-vision-core-20261108` at `7c4d274b214d197b89fd6745305861156684d227`.
- Work branch: `migration/full-rust-20261109`.
- Existing Python implementation and all user data remain untouched during parity work.
- The Rust engine is read-only and paper-only. No signing, broadcasting, private-key/seed handling, or live execution APIs are permitted.
- Existing SQLite files are user data. Migration must be additive and schema-compatible; never rewrite or delete a user's DB during tests.

## Porting order

1. Configuration, RPC transport policy, URL validation, retry/backoff and safe diagnostics.
2. Solana public-address/signature validation and transaction parsing/classification.
3. Signature store, durable observation records, freshness policy, bounded Fresh/History queues and 429 cooldown.
4. Exact-decimal paper simulator, atomic SQLite persistence, idempotency, restart recovery, proportional sell and P&L ledger.
5. Profile store and dashboard API parity.
6. Monitoring lifecycle, pending-order recovery, source-to-committed-fill latency instrumentation.
7. End-to-end and regression tests against the existing Python behavior.
8. Switch the supported runtime to Rust only after parity, CI, and explicit acceptance. Remove Python runtime code only in the final cutover after proof that no supported entrypoint depends on it.

## Acceptance gates

- Offline Rust tests and CI pass.
- No live transaction signing or broadcast capability exists in the dependency graph or source.
- Existing database schema/data remain readable and unchanged by migration tests.
- Buy, partial sell, sell-all, fees, balance, realized/unrealized P&L and duplicate-signal behavior match the paper simulator contract.
- Freshness: maximum source age 60 seconds, future tolerance 5 seconds; old backlog is never copied.
- One worker per cycle; Fresh first, then capped History; newest 1,000 history signatures per wallet; History batch 10–25; HTTP 429 cooldown starts at 30 seconds with exponential backoff up to 10 minutes and honors Retry-After.
- End-to-end source receipt to committed paper fill is measured separately from chain detection; do not claim the <100 ms target without measured P50/P95/P99/max evidence.
- Python files are not deleted until all relevant tests and runtime entrypoints have Rust equivalents.
