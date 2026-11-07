# Engen — Vision Roadmap

**Branch:** `feature/engen-vision-core-20261108`  
**Date:** 2026-11-08  
**Goal:** Build a reliable, ultra-low-latency **Paper Trading Copy Engine** first. Real trading stays disabled and separate.

---

## Vision Summary (Owner Requirements)

1. Monitor public Solana wallets
2. Detect Buy/Sell quickly
3. Automatically create matching **paper** trades
4. Detection → Paper Fill + DB commit in **under 100ms** (target)
5. Persistent balance, positions, P&L, and full audit history (survives restart)
6. Zero duplicate fills
7. Clean professional dashboard
8. 24/7 reliable monitoring + recovery
9. No real signing / broadcast in this phase

---

## Progress on this branch

### Done
- [x] ROADMAP documented
- [x] `paper_copy_runner` hardened: latency hooks, `elapsed_ms` outcomes, `get_pending_copy_status()`
- [x] `monitor_bridge` wires optional latency recorder into queue + execute
- [x] Restart-recovery tests: filled copy survives new simulator instance; pending order fills after restart; latency stages recorded
- [x] `pending_copy_report.attach_pending_copy()` helper for dashboard reports

### In progress / next
- [ ] Wire `attach_pending_copy` into `/api/paper-simulation` responses
- [ ] Dashboard metric: Pending Copy Orders
- [ ] Flatten nested `Engen-main/Engen-main` structure
- [ ] Faster detection path (Yellowstone / gRPC) — later phase

---

## Current State (base + this branch)

### Already present (good foundation)
- Safety boundary (no private keys, no live trading)
- PaperSimulator with SQLite persistence (orders, fills, balances)
- ProfileStore (profiles, source wallets, transactions)
- paper_copy_runner (queue automatic copy orders from classified swaps)
- Transaction classification (BUY_CANDIDATE / SELL_CANDIDATE etc.)
- Many unit tests and CI workflows
- Dashboard (basic paper status, history, wallet watchlist)

### Critical gaps vs vision

| Gap | Severity | Notes |
|-----|----------|-------|
| Automatic copy not fully reliable end-to-end | High | Improved; still depends on market data freshness |
| Nested repo structure (`Engen-main/Engen-main`) | High | Makes development painful |
| Sub-100ms detection-to-fill not measured/achieved | High | Local stage timers exist; not full path yet |
| Dashboard UX vs full vision | Medium | Pending copy UI not fully wired |
| Many stacked / draft PRs | Medium | Hard to know single source of truth |

---

## Safety Rules (Never Violate)

- No private keys / seed phrases
- No transaction signing
- No broadcast of real transactions
- Paper simulation only in this phase
- Live mode requires separate explicit approval and a different architecture

---

**Owner approval required before any live-trading work.**
