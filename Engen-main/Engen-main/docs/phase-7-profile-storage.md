# Phase 7 — persistent profile and portfolio data

## Storage decision (181–182, 185)

Use SQLite through Python's standard-library `sqlite3`. The default database path is `data/profiles.db`; deployments may pass another path to `ProfileStore`. Initialization creates the schema when `PRAGMA user_version` is 0 and records schema version 1. A database with a newer version is rejected rather than silently downgraded. SQLite foreign keys, a 10-second busy timeout, and WAL journaling are enabled. No third-party database dependency is required.

## Version 1 data model (183–185)

- `profiles`: stable UUID profile ID, display name, optional destination public address, disabled/active/archived status, created/updated/deleted timestamps.
- `profile_settings`: one JSON object per profile, stored separately to enforce profile-level isolation.
- `source_wallets` and `profile_source_wallets`: normalized source addresses and many-to-many profile associations. Sharing a source wallet across profiles is allowed (189); each association must be explicit (190).
- `transactions`: globally unique transaction signature, source wallet, slot/block time, UTC observation timestamp, status, classification, evidence JSON, and optional exact raw amount/token mint/decimals.
- `profile_transactions`: explicit profile-to-transaction links, allowing the same observed transaction to be referenced by more than one associated profile without duplicating the transaction row.
- `balance_snapshots`: immutable observation history with profile, wallet, token mint, exact raw amount, decimals, observation timestamp, source/provenance, optional freshness, and current-snapshot marker.

## Validation and isolation (184, 186–194)

Profiles receive generated UUIDs and start disabled. Names are bounded. Settings must be JSON objects and secret-key-like field names are rejected. Settings are updated only by profile ID. Destination public addresses are stored in a dedicated column; no authorization material belongs in profile settings. Source wallet association is explicit and unique per profile/address. The store module does not start monitoring or submit transactions.

## Transactions and balances (195–205)

Transaction signatures are the primary key, so duplicate submissions are idempotent. A repeated signature cannot silently be reassigned to another source wallet. Amounts remain exact strings, token decimals are retained, and timestamps are stored as UTC ISO-8601 text. Classification evidence is persisted as JSON. Balance snapshots retain historical observations; the current marker identifies the latest observation explicitly marked current, while freshness/provenance remains visible to callers. Zero balances are retained as observations instead of deleting the token row. Missing metadata can be represented as `null` in optional transaction fields; balance snapshots require a mint and decimals.

The `portfolio_holdings` method aggregates current observations using exact decimal arithmetic and returns raw amounts, decimals, provenance, observation range, wallet count, and a nullable metadata field. An optional maximum age excludes stale snapshots. Zero balances are retained; a missing snapshot is not interpreted as zero. Token metadata lookup/display conversion belongs in a separate presentation layer.

## Update, deletion, backup and recovery (206–210)

Profile edits update profile metadata and do not delete transaction history. Profile deletion is a soft delete: it archives the profile and sets `deleted_at`; linked transaction and balance rows remain for audit/recovery. Before production use, back up the SQLite database using SQLite's online backup API or a consistent snapshot, include the database/WAL state as appropriate, restrict file permissions, and periodically test restoring a copy. Do not copy a live database file blindly while writes are in progress.

Automated tests cover per-profile settings isolation, shared-source association, duplicate transaction uniqueness, exact amounts and decimals, balance history/current state, soft-delete retention, schema initialization, and persistence across a new store instance. These tests do not by themselves prove production backup restoration or independently verified on-chain balance accuracy.

## Task status

Implemented in this branch: 181–185 (versioned initial schema; future schema changes need versioned migrations), 186–202, 203 (aggregation from stored current snapshots, with freshness filtering), 204, 206–209 (including SQLite online backup and restore test). Partially implemented: 205; missing metadata is represented as null by the storage API, but dashboard rendering is not yet integrated. Task 210 has automated coverage for isolation, uniqueness, persistence, and backup/restore; the GitHub safety/unit workflow passed on the latest documented commit. Production deployment recovery and dashboard integration still require separate validation. Do not treat these offline tests as proof of on-chain data accuracy.
