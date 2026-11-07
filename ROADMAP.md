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

## Current State (as of this branch base)

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
| Automatic copy is not fully reliable end-to-end | High | Orders often stay in WAITING state (price/liquidity) |
| Nested repo structure (`Engen-main/Engen-main`) | High | Makes development and imports painful |
| Sub-100ms detection-to-fill not measured or achieved | High | Still RPC polling style; no Yellowstone/gRPC path |
| Full process restart recovery not fully proven | Medium-High | Component tests exist; full process rehearsal weaker |
| Dashboard not yet matching full vision UX | Medium | Missing clean settings, reset, sell-all, clear P&L view |
| Many stacked / draft PRs | Medium | Hard to know the single source of truth |

---

## Prioritized Work Plan

### Phase 1 — Make Paper Copy Reliable (Current Focus)
1. Stabilize automatic copy pipeline (queue → fill)
2. Guarantee restart-safe paper ledger + source references
3. Strengthen stale-transaction protection
4. Add clear end-to-end offline test: monitor → classify → queue → fill → restart → balances intact
5. Improve observability (latency timers for detection and fill)

### Phase 2 — Clean Structure + Dashboard
1. Flatten nested `Engen-main/Engen-main` structure
2. Make dashboard match vision (wallet management, copy %, P&L, history, reset, sell-all)
3. Single clear entry point and README

### Phase 3 — Latency Path
1. Instrument Detection latency and Detection→Fill latency (P50/P95/P99)
2. Evaluate Yellowstone gRPC / faster feeds
3. Reduce blocking work on the hot path

### Phase 4 — Production Hardening
1. 24/7 monitoring recovery
2. Health checks and alerting
3. Deployment guide (still paper-only)

---

## Safety Rules (Never Violate)

- No private keys / seed phrases
- No transaction signing
- No broadcast of real transactions
- Paper simulation only in this phase
- Live mode requires separate explicit approval and a different architecture

---

## Next Immediate Actions on this branch

1. Improve automatic paper-copy execution reliability
2. Add / strengthen restart recovery tests for paper ledger
3. Add latency measurement points
4. Keep changes small and reviewable

---

**Owner approval required before any live-trading work.**
