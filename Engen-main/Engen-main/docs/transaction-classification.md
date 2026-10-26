# Transaction Classification and Analysis (Tasks 151–180)

## Safety contract

Classification is an analysis result, not an execution command. `BUY_CANDIDATE`
and `SELL_CANDIDATE` are provisional labels. `UNKNOWN` is the required result
when evidence does not distinguish a swap from transfers, rent, liquidity
operations, or other activity. Failed on-chain transactions are labeled
`FAILED_TRANSACTION`; they are not buy/sell candidates. The current engine is
read-only/paper-only and has no live execution path.

## Evidence required

A buy candidate requires wallet-owned balance evidence showing one or more
non-quote token mints increasing while a recognized quote asset (currently
USDC, USDT, wrapped SOL, or a fee-adjusted native SOL movement) decreases.
A sell candidate requires the inverse movement. These balance patterns alone
are not proof of a swap; instruction context, signer identity, and sufficient
confirmation are also considered by the monitor. Native SOL is only a heuristic
quote movement because it can reflect transfers, rent, account creation, or
other uses.

A token receipt alone is never proof of a buy. A SOL decrease alone is never
proof of a sell. The monitored wallet's ownership is matched using the token
balance entry's `owner`; changes with unknown owners or another owner are not
attributed to the monitored wallet. Relevant token mint addresses are compared
before direction is inferred.

## Asset amounts, fees, and rent

- SPL token balances retain raw integer amounts as strings, plus the mint and
  token decimals. A human-readable quantity is derived as raw amount divided by
  (10^{decimals}); raw values remain the source of truth.
- Native SOL balance deltas are represented in lamports (9 decimals). For
  account index zero, the known network fee is added back to the raw delta to
  estimate movement excluding the network fee. The raw delta and fee-adjusted
  delta are both retained. Other accounts are not fee-adjusted.
- Transaction fees are recorded separately in lamports. A fee-only decrease,
  without a non-quote asset movement and recognized counter-movement, is not a
  trade.
- Rent deposits/refunds may appear as SOL movements alongside account
  creation/closure instructions. Rent is not reliably identifiable from
  balance deltas alone; when instruction context is incomplete or ambiguous,
  retain `UNKNOWN` rather than treating the movement as swap consideration.
- Quantity change is not market-value change. No USD value, realized P&L, or
  price impact is inferred without a separate trusted price source.

## Complex and failed transactions

- Aggregate deltas across multiple token accounts by mint only after filtering
  to the monitored wallet. Keep per-account raw changes in evidence.
- Transactions that move several mints or contain multiple instructions are
  considered candidates only if the direction is unambiguous; mixed directions,
  multiple quote types, or unsupported combinations remain `UNKNOWN`.
- Parsed outer and inner instructions/program IDs are preserved as context.
  An instruction name by itself is not proof of an economic swap.
- Ordinary SOL transfers, token transfers, liquidity add/remove operations,
  account setup/closure, airdrops, and other complex operations must not be
  mislabeled as swaps solely from balance signs. If the available metadata does
  not separate these cases, classification remains `UNKNOWN`.
- A non-null transaction metadata `err` means `FAILED_TRANSACTION`. Failed
  transactions may still pay fees, but their attempted balance effects must
  not become successful trade signals.

## Confidence and audit evidence

Every `classify_transaction` result contains a classification, confidence,
reason, asset/quote mint lists, and evidence. Confidence describes confidence
in the interpretation, not trade profitability. Balance-pattern candidates
are marked medium confidence; unknown outcomes are low confidence; a provider-
reported failed status is high confidence as a failure. Evidence retains the
wallet, raw deltas, decimals, source, instruction context, fee, and known
limitations. Missing or incomplete metadata remains pending upstream and is
not treated as a successful classification.

## Regression coverage and limitations

The unit tests cover synthetic buy/sell patterns, token-only receipts, SOL-only
decreases, fee-adjusted native SOL deltas, raw amounts and decimals, multiple
token accounts/mints, other-wallet ownership, missing/mismatched decimals,
ambiguous movements, and failed transaction metadata. These are deterministic
fixtures, not a claim that every real-world DEX, aggregator, liquidity program,
or Token-2022 extension has been validated against a known mainnet transaction.

Classification error rates have not yet been measured against a labeled,
representative real-transaction dataset. Before considering any automated
execution, collect reviewed labels for buys, sells, transfers, rent, liquidity
operations, failed transactions, and unknown cases; report false-buy and
false-sell rates separately and require explicit safety review. Unknown or
ambiguous results must never trigger an automatic live trade.
