# Latency benchmark plan

## Current status

The engine is currently read-only. It fetches public Solana data and analyzes
transactions; it does not sign or submit transactions. Therefore, current
measurements must not be described as real trade-execution latency.

## Latency stages to measure

1. Detection delay: time between a watched wallet's on-chain activity and
   the monitor discovering its signature. Polling interval and RPC service
   behavior affect this value.
2. Signature fetch: duration of the RPC request that fetches recent wallet
   signatures.
3. Transaction fetch: duration of the RPC request that fetches transaction
   details for a signature.
4. Analysis: local time spent validating and classifying transaction data.
5. Signal-to-decision: elapsed time from the monitor receiving a new
   signature to a paper-trading decision being produced.
6. Simulated execution: time spent in a local paper-trading simulator. This
   is not equivalent to a transaction sent to Solana.
7. On-chain confirmation: only measurable after a real signed transaction
   has been submitted in an explicitly approved future phase. It is not part
   of the current read-only benchmark.

## Benchmark rules

- Use a monotonic clock for elapsed time.
- Record multiple samples and report count, mean, P50, P95, P99, and maximum.
- Separate local processing time from RPC/network waiting time.
- Record RPC provider/region and test conditions without logging API keys.
- Compare runs under the same conditions; do not optimize based on one sample.
- Include errors, timeouts, unavailable transactions, and duplicate signatures.
- Never enable transaction signing or submission as part of a performance test.
- Do not claim a 100–200 ms end-to-end target has been met until the relevant
  end-to-end stages have been measured under representative conditions.

## Current instrumentation and remaining work

The bounded, dependency-free recorder in `engine/latency.py` is wired into the
paper monitor pipeline. It records signature-fetch RPC time, transaction-fetch
RPC time, local transaction analysis, full monitor-scan time, and local
paper-decision time when those paths execute.

A live, representative RPC baseline has **not** yet been collected. The
instrumentation and mock-based unit tests do not establish real-world latency.
A benchmark run against a public wallet and public RPC endpoint is still needed.
A single `--once` scan can confirm connectivity and produce initial samples,
but repeated scans and representative activity are needed for useful P50/P95/P99
comparisons. Polling interval also contributes to detection delay and is not
included in the duration of an individual RPC call.

The paper monitor uses a persistent signature database and a persistent paper
ledger by default. For a benchmark, use dedicated temporary database paths so
existing monitoring history and virtual balances are not altered.
