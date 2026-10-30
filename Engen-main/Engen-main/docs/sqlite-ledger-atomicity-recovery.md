# SQLite paper-ledger atomicity and restart recovery

The SQLite ledger stores simulated balances and paper fills only. It does not sign,
submit, or broadcast blockchain transactions.

## Atomic paper fills

- Each paper-processing call acquires a per-instance lock and starts a SQLite
  `BEGIN IMMEDIATE` transaction before reading the current ledger.
- The latest committed balances and processed signatures are reloaded while the
  SQLite write reservation is held. Separate processes therefore do not calculate
  from stale in-memory balances.
- The unique transaction-signature constraint is claimed in the same transaction
  as the virtual balance upserts. A duplicate signature is reported as
  `DUPLICATE` and cannot debit or credit balances a second time.
- If any insert, balance update, or commit fails, SQLite rolls back the complete
  fill. The in-memory ledger is refreshed only after commit succeeds.
- WAL journaling, FULL synchronous mode, and a bounded 10-second busy timeout
  are configured for the file-backed ledger.

## Signature ingestion

`SignatureStore.save_many` validates the full batch and inserts it in one
transaction. A database error rolls back the whole batch rather than leaving a
partially saved set. The composite primary key continues to make repeated
wallet/signature pairs idempotent.

## Recovery

On startup and before every persisted paper fill, `PaperTrader` reloads balances,
trades, and processed signatures from SQLite. The one-time initialization marker
prevents restart-time initial balances from overwriting the saved ledger. A
signature already committed before shutdown is recognized as a duplicate after
restart; a transaction whose write did not commit leaves neither its fill nor
its balance changes in the database.

## Verification

The regression suite covers rollback after an injected persistence failure,
concurrent duplicate execution across separate trader instances, concurrent
distinct fills against a shared balance, process restart recovery, duplicate
signature persistence, and atomic signature batches. GitHub Actions repeats the
SQLite-specific tests ten times as well as running the complete offline suite.

These guarantees apply to the local SQLite paper ledger. They do not make external
Solana transactions atomic, and no live-trading path is enabled by this work.
