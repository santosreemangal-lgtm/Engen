# Python → Rust Migration

**Branch:** `feature/rust-engine-core`  
**Rule:** Paper only until `LIVE_BOUNDARY.md` / `LIVE_READINESS.md` are satisfied.

## Can we convert all 7 files at once?

**Not in one step.** Rough size of the current Python side:

| Module | Role | Rust priority |
|--------|------|----------------|
| `transaction_monitor.py` | Detect source txs | **P0** (latency) |
| `paper_copy_runner.py` | Queue + execute paper copy | **P0** |
| `paper_simulator.py` | Virtual ledger / fills | **P1** |
| `monitor_bridge.py` | Wire monitor → paper | **P1** |
| `monitor_service.py` | Background scan loop | **P1** |
| `profile_store.py` | Profiles SQLite | **P2** |
| `dashboard/server.py` | HTTP UI/API | **P3** (keep Python longer) |

Dashboard HTTP + HTML is **not** where <100ms is won. Winning path is **detection → paper decision**. Keep Python dashboard; put hot path in Rust.

## Recommended architecture (hybrid)

```
[Rust: monitor + paper core]  --JSON/stdio or local HTTP-->  [Python: dashboard]
```

- Rust binary: watch wallets, classify swaps, paper queue/fill, SQLite ledger  
- Python: existing `server_with_pending.py` UI until a Rust HTTP layer is worth it  

## Phases

### Phase R1 (this branch scaffold)
- [x] `rust/engen_core` Cargo crate layout
- [x] Module map matching Python names
- [x] Paper-only stubs (no signing / broadcast)

### Phase R2
- [ ] `paper_simulator` SQLite ledger parity with Python tests
- [ ] `paper_copy_runner` queue + dedupe + stale gate

### Phase R3
- [ ] `transaction_monitor` RPC or Yellowstone gRPC subscribe
- [ ] `monitor_service` + bridge loop

### Phase R4 (optional)
- [ ] `profile_store`
- [ ] Axum/Actix dashboard **or** keep Python UI

## Do not

- Delete working Python paper engine until Rust passes the same tests  
- Put private keys or `sendTransaction` in this crate  
