# Real-Transaction Validation Plan (Tasks 178–179)

## Target wallet
- Label supplied by project owner: Savannah
- Public address: `ardinRsN1mNYVeoJWTBsWeYeXvuR9UUDGMsCDKpb6AT`
- Data access: public on-chain data only; read-only
- Safety boundary: no signing, no wallet secret, no live trading

## Current status
The synthetic regression tests pass in GitHub Actions on feature branch
`audit/safety-hardening-2026-10-26`. This is not yet a real-world accuracy
measurement. No verified transaction labels have been collected in this file,
so no false-buy rate, false-sell rate, or real-world accuracy is claimed.

## Validation procedure
1. Collect a bounded sample of recent confirmed transaction signatures for the
   target public address using the existing read-only RPC path.
2. Fetch transaction metadata only. Preserve signature, slot, success/failure,
   fees, account keys, outer/inner instructions, and pre/post SOL and token
   balances. Do not store secrets (none are needed).
3. Run the engine classifier and save its predicted label and evidence.
4. Independently review each sampled transaction against the raw instruction
   and balance-change evidence. Mark a label as reviewed only when evidence
   supports it; otherwise use `UNKNOWN`. Do not treat the engine's own output
   as the ground-truth label.
5. Put reviewed rows in `docs/transaction-validation-template.csv` (or a copy
   outside the repo). Run:
   `python tools/evaluate_transaction_labels.py path/to/reviewed.csv`
6. Report sample size, label distribution, exact-label accuracy, false-buy and
   false-sell counts, BUY/SELL precision and recall, and UNKNOWN rate. Keep
   unreviewed rows out of metrics. Describe the sample window and limitations.

## Labeling rules
- `BUY_CANDIDATE` / `SELL_CANDIDATE`: use only when the evidence supports a
  successful swap direction, not merely a token receipt or SOL decrease.
- `TRANSFER`: asset movement is a transfer rather than a swap.
- `RENT`: rent/account-lifecycle movement is sufficiently identified.
- `LIQUIDITY_OPERATION`: liquidity add/remove is sufficiently identified.
- `FAILED_TRANSACTION`: confirmed transaction metadata reports failure.
- `UNKNOWN`: evidence is incomplete, mixed, or ambiguous.

## Limits
A wallet address identifies the address to inspect, not the intent behind every
transaction. Explorer labels can help review but are not automatically ground
truth. A small sample is a preliminary check, not a statistically strong
accuracy guarantee. UNKNOWN is a safe outcome and must never be converted into
an automatic live trade.
