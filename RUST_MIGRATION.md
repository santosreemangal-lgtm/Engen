# Python → Rust Migration

**Branch:** `feature/rust-engine-core`  
**Goal:** Run the copy engine in Rust. Python stays until full swap-classification parity.

## Progress

| Python module | Rust module | Status |
|---------------|-------------|--------|
| `paper_simulator.py` | `paper_simulator.rs` | Fill BUY/SELL, fees, slippage, SQLite restart |
| `paper_copy_runner.py` | `paper_copy.rs` | Queue, stale gate, execute pending |
| source freshness | `source_freshness.rs` | Done |
| `profile_store.py` | `profile_store.rs` | SQLite profiles |
| `transaction_monitor.py` | `transaction_monitor.rs` | Read-only `getSignaturesForAddress` + classified ingest. Does **not** invent trades from signatures |
| `monitor_service` / bridge | `monitor_service.rs` | Tick queue + fill |
| dashboard API | `paper_http.rs` | Paper JSON: `/health`, `/api/paper`, `/api/candidates`, `/api/fill` |
| swap classification | still Python | Not ported — needs token-balance parser |
| Yellowstone gRPC | not started | Needs a provider token; polling RPC is the read-only path for now |
| `dashboard/server.py` UI | Python | Keep until classification moves |

## Verify

```bash
cd rust/engen_core
cargo test
cargo run --bin engen_paper
```

`cargo test` covers ledger restart, profile reopen, stale gate, monitor tick fill, and paper-only health.

## Still later

1. Port Python swap classification (token deltas → BUY/SELL) into Rust
2. Optional Yellowstone gRPC subscribe behind the same candidate ingest
3. Point the Python dashboard at the Rust paper API
4. Only then retire the Python ledger

## Safety

No private keys, no signing, no broadcast. Signature polling is read-only. Unparsed signatures never become paper orders.
