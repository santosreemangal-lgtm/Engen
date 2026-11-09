# Rust Backend Migration — Status

**Target baseline:** `feature/engen-vision-core-20261108`  
**Migration branch:** `rewrite/rust-backend-20261109`  
**Policy:** Rust becomes the only backend/engine implementation after feature parity and verification. The HTML/CSS/JavaScript browser UI remains browser-native; Rust serves the UI and its API.

## Safety invariants

- Public Solana RPC reads only.
- Paper orders, fills, balances, and P&L remain virtual.
- No private-key or seed-phrase ingestion, storage, or logging.
- No signing, transaction construction for live execution, or transaction-broadcast API.
- Monitoring remains opt-in and binds to loopback by default.
- Freshness: source block time must be present, no more than 60 seconds old, and no more than 5 seconds in the future.
- Preserve exact token raw amounts/decimals and use decimal-safe accounting.
- Never delete or migrate user `data/` databases as part of a code migration.

## Ported in the first Rust foundation batch

- Strict safe-mode configuration.
- Unconditional live-execution denial.
- Source timestamp freshness checks.
- Base58 validation for 32-byte public keys and 64-byte signatures.
- RPC URL and timeout validation.
- Bounded latency samples and nearest-rank P50/P95/P99 summaries.

## Not yet ported — do not remove the Python implementation

1. JSON-RPC HTTP client, retry/backoff, Retry-After and rate-limit handling.
2. Signature store, pending queue, retry state, durable deduplication and migrations.
3. Transaction parsing/classification with evidence and explicit UNKNOWN outcomes.
4. Profile store, source-wallet associations, profile settings and recovery/backup.
5. Paper order queue, proportional sells, fills, atomic SQLite accounting and restart recovery.
6. Market-data provider and price freshness.
7. Monitoring workers, one-worker-per-cycle scheduling, Fresh-first/History-capped flow and 429 cooldown.
8. Dashboard HTTP API, authentication, origin checks, body limits, rate limiting and static UI serving.
9. Telegram paper-only bot and utility/analysis scripts.
10. Rust unit, integration, fault-injection, E2E, restart, and CI verification replacing the Python suite.
11. Entrypoints, Render configuration, Codespaces instructions, and deployment documentation.

## Completion rule

A Python module may be removed only after its Rust equivalent has:
- the same externally observable behavior or a reviewed API compatibility change;
- unit and integration coverage for normal, boundary, and failure cases;
- restart/data-compatibility tests where persistence is involved;
- passing Rust CI on the migration branch.

No user database is opened, rewritten, deleted, or migrated by the source-code port itself.
