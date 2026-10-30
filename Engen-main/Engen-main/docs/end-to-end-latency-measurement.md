# End-to-end paper-pipeline latency

The dashboard exposes `GET /api/latency`. It reports bounded in-memory samples
from the current server process. Samples reset on process restart and are not
persisted to SQLite.

## Measured stages

- `wallet.signature_fetch`: public `getSignaturesForAddress` RPC duration.
- `transaction.rpc_fetch`: public transaction metadata RPC duration.
- `transaction.local_analysis`: local transaction decoding and balance analysis.
- `transaction.process_total`: complete per-signature processing, including RPC,
  local analysis, confirmation checks, and status persistence performed inside
  the processing call.
- `pipeline.profile_persistence`: optional profile-history persistence stage.
- `paper.order_queue`: validation and creation of eligible virtual paper orders.
- `paper.order_execution`: market-data validation and virtual paper-fill attempt.
- `pipeline.scan_total`: complete dashboard wallet scan, including discovery,
  processing, persistence, queueing, and paper execution.

Each stage reports sample count, mean, max, p50, p95, and p99 milliseconds.
The recorder is thread-safe and retains at most 10,000 samples per stage.

## Per-transaction chain-to-decision age

When a Solana `blockTime` is available, a result includes
`latency.chain_to_paper_decision_ms`. This is wall-clock age from the provider's
blockTime to the end of the paper decision scan. It is approximate: blockTime
has second-level precision and depends on provider clock accuracy. It is not
pure engine processing time and must not be interpreted as a guaranteed
transaction-submission or network execution latency.

## Interpretation and safety

All measurements cover read-only RPC and local paper simulation only. No
transaction is signed or broadcast. The dashboard response explicitly reports
`live_execution_latency: false` and `transaction_broadcast: false`.

Use p50 for typical behavior and p95/p99 to understand tail latency. Compare
samples only when the RPC provider, region, workload, and monitoring interval
are comparable. A small sample count is not a reliable performance benchmark.
