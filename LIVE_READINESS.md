# Live Readiness Checklist

Use this **before** any real-fund pilot.  
**None of these items authorize live trading by themselves.**

See also `LIVE_BOUNDARY.md`.

---

## A. Paper engine must already be solid

- [ ] Paper copy runs for several days without silent data loss
- [ ] Restart recovery tests pass (`test_paper_copy_restart`)
- [ ] No duplicate fills for the same source signature
- [ ] Stale source gate active (60s window)
- [ ] Pending + filled history visible on dashboard
- [ ] Virtual balance / P&L numbers match manual spot checks

## B. Architecture separation

- [ ] Live code lives outside paper modules
- [ ] Live process is not the same as `server_with_pending`
- [ ] Paper DB path ≠ live state path
- [ ] Documented kill switch (stop process + disable env flags)

## C. Keys and access

- [ ] No keys in git, screenshots, or chat logs
- [ ] Keys only via env / HSM / hardware wallet
- [ ] Dashboard not exposed publicly without auth + HTTPS proxy
- [ ] RPC endpoints reviewed (no credentials in URLs committed to git)

## D. Risk limits (must be coded before pilot)

- [ ] Max notional per trade
- [ ] Max daily loss / max open positions
- [ ] Mint allowlist (or explicit deny of unknown mints)
- [ ] Slippage / liquidity minimums
- [ ] Dry-run mode that builds but does not send transactions

## E. Pilot rules

- [ ] First pilot uses **tiny** size only
- [ ] Owner watches the first sessions live
- [ ] Rollback plan written (how to stop and what to check on-chain)
- [ ] Written owner approval for that pilot date/size

## F. Performance (only if <100ms is a hard requirement)

- [ ] Detection path measured on real feed (not only local paper timers)
- [ ] P50 / P95 / P99 logged for detection → decision
- [ ] If polling cannot meet target, plan gRPC/shred sidecar (often Rust)

---

## Status on this branch

| Area | Status |
|------|--------|
| Paper checklist (A) | Largely in place; still needs your multi-day run |
| Live architecture (B–E) | **Not built** — by design |
| Sub-100ms live proof (F) | **Not done** |

**Do not enable real execution until A–E are explicitly completed and approved.**
