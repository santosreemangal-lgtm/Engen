# Python → Rust Migration

**Branch:** `feature/rust-engine-core`  
**Goal:** Ultimately run the copy engine in Rust. Python stays until parity.

## Progress

| Python module | Rust module | Status |
|---------------|-------------|--------|
| `paper_simulator.py` | `paper_simulator.rs` | **R2 advanced** — create/fill BUY·SELL, fees, slippage, optional SQLite |
| `paper_copy_runner.py` | `paper_copy.rs` | Queue + stale gate + execute_pending |
| `source_freshness` | `source_freshness.rs` | Done + unit tests |
| `transaction_monitor.py` | `transaction_monitor.rs` | Stub (R3: RPC/gRPC) |
| `monitor_service` / bridge | `monitor_service.rs` | Stub tick loop |
| `profile_store.py` | `profile_store.rs` | Stub |
| `dashboard/server.py` | — | Keep Python UI for now |

## Run (when Rust toolchain available)

```bash
cd rust/engen_core
cargo test
cargo run --bin engen_paper
```

## Phases

- [x] R1 scaffold
- [x] R2 paper ledger fill path (in progress → solid)
- [ ] R2b SQLite restart tests parity with Python
- [ ] R3 transaction monitor (RPC then Yellowstone gRPC)
- [ ] R3 monitor service production loop
- [ ] R4 profile store + optional Rust HTTP API
- [ ] Cut over: Python dashboard talks to Rust core (JSON/HTTP)

## Safety

No private keys, no signing, no broadcast in this crate.
