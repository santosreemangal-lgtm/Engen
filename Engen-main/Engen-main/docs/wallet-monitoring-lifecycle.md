# Wallet Monitoring and Transaction Processing Lifecycle

This implementation is read-only. It observes public Solana data and can feed
only the paper-trading simulator; there is no live execution path.

## Per-wallet lifecycle

1. Validate the public wallet address as a Base58 value decoding to 32 bytes.
2. On first scan, establish a historical baseline and mark those existing
   signatures processed without creating trade signals.
3. Poll `getSignaturesForAddress` newest-first. After a baseline exists, page
   backwards up to five pages per scan until the last successfully processed
   signature is found. The page cap bounds provider load.
4. Persist each valid 64-byte Base58 transaction signature under the composite
   key `(wallet_address, signature)`; slot is stored when available.
5. Fetch and interpret transaction data separately from signature discovery.
   Missing or incomplete metadata remains pending and can be retried.
6. Preserve public raw transaction data and an explicit processing state for
   later investigation. Never store signing keys or wallet secrets.
7. Run the paper simulator only for available, successful transactions with
   sufficient metadata and a supported candidate. Mark the signature processed
   after paper processing returns.

## Recovery and idempotence

- SQLite uniqueness on `(wallet_address, signature)` prevents duplicate
  discovery records. The wallet position advances monotonically by slot only
  after a signature is marked processed.
- A restart reloads pending signatures from SQLite. Repeated discovery is safe.
- Paper-trade ledger writes update virtual balances and insert the unique
  signature trade in the same SQLite transaction. The unique signature prevents
  a second paper fill after a restart.
- Recoverable RPC or interpretation failures are recorded with stage, code,
  bounded message, attempt count and last-check time. After five failures, a
  record moves to `needs_review` and is excluded from automatic retries.
- Incomplete metadata is not sent to the paper simulator. It remains pending
  until repaired or moved to review after repeated attempts.
- One monitoring service worker polls wallets sequentially. Starting an already
  active service updates its wallet set rather than creating another worker.
  Each wallet has independent state and error reporting.
- Last successful check time is separate from the last attempted scan. A monitor
  becomes `stale` when its last successful check is more than two polling
  intervals old; scan failures are reported independently.
- Failed on-chain transactions remain distinguishable from successful
  transactions. Network/RPC failures do not create fabricated successful
  transaction records.

## Data minimization and privacy

Only public wallet addresses, public signatures, provider observations, raw
public transaction payloads, and structured processing metadata are retained.
Provider errors are sanitized; raw environment configuration and secrets are
not logged. Retained raw transactions are idempotently keyed per source wallet
and signature.

## Checklist mapping (121–150)

- 121–125: existing monitor reviewed; per-wallet states are isolated; address
  validation and multiple-wallet service support.
- 126–128: bounded polling/pagination, processed-position tracking, SQLite
  recovery after restart.
- 129–132: unique signature keys, idempotent paper ledger, serial polling and a
  single lifecycle-managed background worker.
- 133–135: per-wallet failure isolation, last-successful-check timestamps, stale
  monitor reporting.
- 136–138: discovery, interpretation, paper simulation and future live execution
  remain separate layers; live execution is absent.
- 139–141: signature validation before monitor persistence, original public
  transaction retention, no signing credentials or unnecessary secrets.
- 142–144: failed transactions are distinguished, incomplete metadata remains
  retryable, and unique transaction keys prevent duplicate effects.
- 145–148: structured stage errors, bounded retries, review state after five
  failures, and atomic paper-ledger persistence.
- 149–150: regression tests cover pagination/recovery, state isolation,
  idempotency, and the read-only/paper-only boundary.
