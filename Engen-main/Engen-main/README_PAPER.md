# Engen Paper Copy Engine (this branch)

High-level paper-only Solana copy engine. **No private keys. No signing. No broadcast.**

## Quick start

From repository root:

```bash
python run_dashboard.py
```

From `Engen-main/Engen-main`:

```bash
python run_dashboard.py
# or
python dashboard/server_with_pending.py
```

Then open the localhost URL printed in the log (default `http://127.0.0.1:8000`).

## What you get

1. Read-only public wallet monitoring
2. Automatic **paper** copy orders (BUY/SELL candidates)
3. Persistent SQLite paper ledger (balances, orders, fills, P&L)
4. Restart-safe ledger (see unit tests)
5. Pending copy count on API + dashboard UI
6. Local latency timers for queue / price / execute stages

## Important files

| File | Purpose |
|------|---------|
| `dashboard/server_with_pending.py` | Canonical dashboard (pending API + UI inject) |
| `dashboard/pending_copy_ui.js` | Pending metric in browser |
| `engine/paper_copy_runner.py` | Queue + execute paper copies |
| `engine/pending_copy_report.py` | Attach pending_copy to reports |
| `engine/paper_simulator.py` | Virtual balances / fills |
| `run_dashboard.py` | One-command launcher |

## Safety

- `ENGINE_MODE` / live trading must stay disabled
- Dashboard binds to localhost by default
- Market data is public snapshot only; not guaranteed executable quotes

## Language

Python is intentional for this paper phase. A Rust detection sidecar can be added later without rewriting the paper ledger.

## Tests (when you can run them)

```bash
cd Engen-main/Engen-main
python -m unittest engine.engine.engine.tests.test_paper_copy_restart -v
python -m unittest engine.engine.engine.tests.test_pending_copy_report -v
```

See also repo root `STATUS.md` and `ROADMAP.md`.
