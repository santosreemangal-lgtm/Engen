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

## Current limitation

The engine/latency.py module provides a bounded, dependency-free timing recorder
for local measurements and unit tests. It is not yet wired into the full monitor
pipeline, so this document is a measurement plan rather than a claim of measured
end-to-end performance.
