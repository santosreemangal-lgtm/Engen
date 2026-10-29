# Phase 8 task status — P&L, paper trading, and simulation

Branch: `feature/phase-7-profile-storage`  
Implementation: `engine/pnl.py`, `engine/paper_simulator.py`  
Tests: `engine/engine/engine/tests/test_phase8_pnl_simulation.py`  
Dashboard report: `dashboard/server.py` endpoint `/api/paper-simulation`; UI panel in `dashboard/index.html`

- [x] **211.** Realized P&L is net sale proceeds minus average-cost basis allocated to sold quantity.
- [x] **212.** Unrealized P&L is fresh USD market value minus remaining known cost basis; unavailable if basis/price is unknown.
- [x] **213.** Selected pooled average-cost basis.
- [x] **214.** Documented average-cost formulas and limitations in `docs/phase-8-pnl-paper-trading.md`.
- [x] **215.** Purchase cost is quantity × sourced USD unit price, plus separate costs unless already included.
- [x] **216.** Sale proceeds are quantity × execution price, less separately charged fees/costs.
- [x] **217.** Supports separately stated trading fees and network costs.
- [x] **218.** Explicit `fee_included` prevents charging a fee twice.
- [x] **219.** Deposits and withdrawals are non-trading cash flows, not realized trading P&L.
- [x] **220.** Same-owner internal transfers are recorded without changing aggregate portfolio quantity or creating P&L.
- [x] **221.** Airdrops/unsolicited receipts have unknown basis unless acquisition value is independently supplied.
- [x] **222.** Missing historical prices return unavailable P&L, not zero.
- [x] **223.** Market prices require a source and timezone-aware observation timestamp; this version accepts a supplied price object and has no connected price-feed provider.
- [x] **224.** Price age is reported in seconds and freshness is configurable (default 60 seconds).
- [~] **225.** The simulator now fails closed by default when liquidity data is missing, stale, or below a configurable USD minimum (default $10,000); liquidity observations require their own source and timezone-aware timestamp. Automated tests cover missing, stale, below-threshold, and sufficient liquidity. Still partial: no independent production liquidity provider is connected, so this safety gate only works when a caller supplies trustworthy liquidity observations.
- [x] **226.** Unknown acquisition basis remains unknown and never becomes a fabricated zero-cost position.
- [x] **227.** USD is the only supported portfolio valuation currency; other currencies require a sourced conversion before use.
- [x] **228.** `Decimal` arithmetic is used without intermediate rounding; results are returned as decimal strings.
- [x] **229.** Known-input realized-profit and realized-loss tests added.
- [x] **230.** Known-input unrealized-P&L tests added, including stale/missing prices.
- [x] **231.** Tests cover fee inclusion and network-cost handling.
- [x] **232.** Tests separately cover deposits, withdrawals and internal transfers.
- [x] **233.** Added a paper simulator with no RPC, signing, or broadcast code.
- [x] **234.** Deterministic slippage defaults to 50 bps (configurable).
- [x] **235.** Simulated trading fee defaults to 10 bps; network cost is a separately supplied USD amount.
- [x] **236.** Tracks created, filled, rejected and cancelled orders plus fills.
- [x] **237.** Signal IDs are unique and duplicate signals cannot create another order/fill.
- [x] **238.** Dashboard displays paper balances, orders, fills and realized P&L separately from real wallet monitoring/balances.
- [x] **239.** Tests cover stale prices, provider/RPC failures, insufficient balances, cancellation, and restart persistence.
- [x] **240.** Realized paper P&L is replayed from persisted fills with fixed cost-basis/slippage/fee rules and verified across restart; unrealized P&L stays explicitly unavailable until a verified fresh price provider is connected.

## Verification note

Do not mark the entire phase production-ready yet: task 225 remains partial because no independent production liquidity provider or live market-price provider is connected. The paper simulator now rejects missing/stale/low liquidity metadata by default, but caller-supplied metadata is not independently verified by this repository. All calculations are offline/simulation-only. Live trading remains disabled.
