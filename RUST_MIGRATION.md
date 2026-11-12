# Python → Rust migration

**Branch:** `feature/rust-engine-core`  
**Binary:** `engen_paper`  
**Tests:** `cargo test` in `rust/engen_core` (20 passing)

Also on the binary:

- API rate limit: 120 requests / 60s (`429`)
- `GET /api/profile-transactions` from `observed_swaps`
- `POST /api/classify` stores a row when `signature` is present
- Native SOL quote (`source=native_sol`, decimals 9) and mixed SOL+USDC rejection

Still Python-only: the large dashboard HTML, full Pump.fun log parser, Yellowstone gRPC, and the old unittest files. Do not delete those until a cutover.


Dashboard-compatible paper routes on the same binary:

- `GET /api/paper-simulation` and `GET /api/paper`
- `GET/POST /api/wallets`
- `GET/POST /api/profiles`
- `GET /api/monitor`
- `GET /api/rpc-health`
- `POST /api/classify` (USDC/USDT/wSOL delta heuristic)
- Optional `DASHBOARD_AUTH_TOKEN` bearer check

Still not a drop-in of `dashboard/server.py` or the full `transaction_analysis.py` (history, rate limits, native-SOL edge cases, gRPC). Those Python files stay until that parity exists.


## Unified paper runtime (this commit)

One process, no Python required to run paper mode:

| Concern | Rust module |
|---------|-------------|
| Bootstrap | `src/main.rs` |
| Config | `src/config.rs` (`ENGEN_LIVE_ENABLED=true` is refused) |
| Logging | `src/logutil.rs` |
| Errors | `src/error.rs` |
| Paper ledger + SQLite | `src/paper_simulator.rs` |
| Copy queue | `src/paper_copy.rs` |
| HTTP API + tiny UI | `src/paper_http.rs` |
| Monitor loop | `src/monitor_service.rs` |
| Read-only RPC signatures | `src/transaction_monitor.rs` |
| Signature dedupe | `src/signature_store.rs` |
| Swap classifier | `src/classifier.rs` |
| Profiles | `src/profile_store.rs` |
| Background worker | `src/worker.rs` |
| Stale source gate | `src/source_freshness.rs` |

```bash
cd rust/engen_core
cargo test
ENGEN_BIND=127.0.0.1:8787 cargo run --bin engen_paper
```

Open `http://127.0.0.1:8787/`.

## Not a full replacement of the Python tree yet

These Python pieces are still the richer implementation. Deleting them now would drop behavior this binary does not have:

- Full dashboard (`dashboard/server.py`, auth, wallet UI, history tables)
- Full Solana transaction parser / labeler (`transaction_analysis.py`, `transaction_processor.py`)
- Yellowstone gRPC / sub-100ms detection
- Market-data provider, PnL reports, Telegram, the old unittest suite

The Python tree stays in the repo until those paths have Rust parity and the same tests. This binary does not sign or broadcast.
