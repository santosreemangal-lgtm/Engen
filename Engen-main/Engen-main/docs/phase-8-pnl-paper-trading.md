# Phase 8 — P&L, paper trading, and simulation

## Accounting contract (tasks 211–232)

- **Currency:** USD is the sole reporting currency for this version. A non-USD asset must have a sourced USD price before it can be valued. The engine does not invent FX conversions.
- **Cost basis:** pooled average cost. A buy's cost basis is quantity × unit price, plus separately supplied trading fee unless the caller explicitly states that the price/amount already includes that fee, plus a separately supplied network fee. Internal arithmetic uses `Decimal`; values are not rounded during calculations.
- **Realized P&L:** net sale proceeds minus the average-cost basis allocated to the sold quantity. Net proceeds equal quantity × execution price, minus a separately charged trading fee unless that fee is already included in the supplied execution price/proceeds, minus separately supplied network cost.
- **Unrealized P&L:** (current quantity × fresh USD market price) − remaining cost basis. The result includes the price source, timestamp and age. The default freshness threshold is 60 seconds.
- **Unknown basis or price:** P&L is unavailable (`null` with an explicit status), never zero. Airdrops and deposits without a documented acquisition value create unknown basis. A position with any unknown-cost units cannot claim a calculated average-cost P&L until that position returns to zero and a new known-basis position is established.
- **Market data:** every `MarketPrice` requires token mint, positive price, USD currency, source and timezone-aware ISO-8601 timestamp. Missing, mismatched, stale or illiquid-token prices are not filled with guessed values.
- **Deposits/withdrawals:** tracked as non-trading flows with zero trading P&L. Known-cost deposits can add basis; unknown-cost deposits make basis incomplete. Withdrawals reduce quantity and allocate known average cost when possible; they do not realize a sale.
- **Same-owner internal transfers:** recorded as a non-P&L event; this portfolio-level ledger does not remove/re-add the quantity, so the transfer is not misreported as a sale or purchase.
- **Airdrops/unsolicited receipts:** quantity is recorded, but no zero cost is fabricated. Until acquisition basis is independently known, realized/unrealized P&L remains unavailable for the affected pooled position.
- **Fees:** the API makes fee inclusion explicit to prevent double counting. Trading fees and network costs are distinct inputs. Fees must be expressed in the USD reporting currency by the caller; no token-to-USD conversion is guessed.
- **Precision/display:** calculations use `Decimal` and return decimal strings. A UI may round for display, but the stored/calculated value must not be rounded first.
- **Limitations:** average cost is a bookkeeping method, not tax advice. Prices and fees are only as reliable as their supplied source. This version has no price-feed connector and does not independently verify market liquidity or USD conversions.

## Paper simulator (tasks 233–240)

`engine/paper_simulator.py` is a separate, deterministic, SQLite-persistent simulator. It contains no RPC, wallet-signing or transaction-broadcast implementation. Orders are created first and then simulated, allowing pre-execution cancellation. Outcomes include `FILLED`, `REJECTED`, and `CANCELLED`; repeated signal IDs are idempotently rejected as duplicates.

- Reference prices are caller-supplied `MarketPrice` values with source and timestamp.
- Default simulated slippage is 50 basis points: buy execution price = reference × (1 + slippage); sell execution price = reference × (1 − slippage).
- Default simulated trading fee is 10 basis points of gross quote value; simulated network cost is a separately supplied fixed amount in USD.
- Stale/missing/mismatched prices, provider/RPC failures, insufficient balances, and costs exceeding proceeds reject the order without creating a fill.
- The database stores orders, fills, price source/time/age, reference/execution prices, slippage, trading fee and network cost. Restart tests ensure balances, fills and duplicate protection survive.
- The dashboard's `/api/paper-simulation` endpoint opens the simulation database read-only and never creates it. The dashboard shows virtual balances and fills in a panel explicitly separated from real wallet balances.
- No simulated P&L is presented as real-wallet P&L. A market-price feed must be independently configured and verified before trusting valuation results.

## Verification

Run from `Engen-main/Engen-main`:

```bash
python -m unittest discover -s engine/engine/engine/tests -v
python -m compileall -q main.py engine dashboard
```

The Phase 8 tests use fixed known inputs and timestamps. They cover realized profit/loss, unrealized P&L, stale/missing prices, unknown airdrop basis, fee double-count prevention, network costs, transfers and cash flows, deterministic slippage, rejected/cancelled orders, duplicate signals, persistence across restart, and explicit separation of simulated balances from real wallet balances.
