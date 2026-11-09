# Live Boundary — Paper vs Real Execution

**Rule:** The current paper system must never gain silent live capability.

## Allowed in paper engine (current branch)

- Read-only public RPC / public wallet monitoring
- Paper order queue, fill, balances, P&L in SQLite
- Dashboard on localhost (or proxy + auth)
- Latency timers, pending status, stale-source gate
- Unit tests and offline verification

## Forbidden in paper modules

| Forbidden | Why |
|-----------|-----|
| Private keys / seed phrases in code or git | Leak risk |
| Transaction signing | Real funds at risk |
| `sendTransaction` / broadcast | Irreversible |
| Sharing paper DB with a live wallet | Accidental mix |
| “Just one flag” that turns paper into live inside the same process | Too easy to misconfigure |

## If live is ever built later

1. **Separate package/directory** (e.g. `live_execution/`), not inside `paper_*` paths
2. **Separate process** and config from the paper dashboard
3. **Explicit env** such as `ENGEN_LIVE_ENABLED=true` plus a second confirmation flag
4. **Hard limits:** max size, max daily loss, mint allowlist, kill switch
5. **Keys only** via OS env / HSM / hardware wallet — never committed
6. **Owner written approval** recorded before any pilot with real funds

## Current default

`ENGEN_LIVE_ENABLED` is **not implemented** and must stay **off / absent**.  
Paper entrypoints (`run_dashboard.py`, `python -m dashboard`, `server_with_pending.py`) do not sign or broadcast.
