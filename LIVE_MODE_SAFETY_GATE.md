# Future Live-Mode Safety Gate — Not an Activation Guide

**Status: LIVE MODE IS NOT IMPLEMENTED OR AUTHORIZED.** This document defines requirements that must be satisfied in a separate reviewed change before anyone considers signed transactions. It does not enable wallet signing, transaction broadcast, or real-money trading.

## Mandatory release gate

A future live-mode proposal must remain disabled by default and fail closed unless every control below is configured, validated, tested, and explicitly approved by the project owner. Paper trading and public-wallet monitoring must continue to work without a private key.

1. **Separate authorization:** a distinct explicit owner approval for the exact release and environment; no configuration flag alone can constitute approval. Never request or log recovery phrases.
2. **Transaction preview:** show network/cluster, token mint, source and destination, exact input/output amounts, fees, slippage bound, price timestamp, and all program instructions before signing. Reject if the preview differs from the final transaction.
3. **Order-size limit:** require a configured maximum per order in the quote asset and a maximum total exposure per destination profile. Missing, zero, malformed, or exceeded limits block execution. Do not provide permissive defaults.
4. **Slippage and price freshness:** require a configured maximum slippage in basis points and a maximum price age. Missing price, stale price, unsupported token, route mismatch, or quote expiry blocks execution.
5. **Duplicate prevention:** persist a unique intent ID and idempotency key before execution; repeated intents must return the prior result rather than submit another transaction. Reconcile uncertain outcomes with the chain before retrying.
6. **Emergency stop:** a durable global kill switch and per-profile pause must be checked immediately before every signing/submission attempt. A stop request must prevent new attempts even during an RPC retry or reconnect loop.
7. **RPC fail-closed behavior:** timeouts, rate limits, malformed RPC responses, stale slots, inconsistent balances, or RPC outage must never bypass limits or cause blind resubmission. Stop new execution until health and transaction state are reconciled.
8. **Wallet authorization:** use a deliberate wallet-provider signing request and display the transaction preview. Never collect seed phrases/private keys in dashboard forms or logs. Do not sign without fresh explicit authorization.
9. **Audit trail:** record intent ID, sanitized preview, limits applied, approvals, outcome and chain signature without secrets. Logs must redact credentials and provider URLs that include credentials.
10. **Independent verification:** unit, integration, fault-injection, duplicate-submission, kill-switch, stale-price, slippage, maximum-exposure, restart-recovery, and testnet-only end-to-end tests must pass before any production review.

## Configuration policy for any future proposal

No live-mode defaults are active in this repository. A future implementation must require explicit, validated settings for maximum order size, maximum profile exposure, maximum slippage (basis points), maximum price age, RPC timeout/retry policy, and kill-switch state. Missing or invalid settings must disable execution. Numeric values must be bounded and validated server-side; the UI cannot be the only enforcement layer.

## Approval and release

Before activation, produce a separate threat model and evidence report, review it with the owner, and obtain explicit approval for the exact reviewed commit. Keep the feature flag off until that approval exists. No approval is implied by this design document, CI success, or prior permission to edit repository files.

## Current status

The current engine remains read-only/paper-trading. There is no supported live signing/broadcast path, so order limits and transaction preview are requirements for a future separate implementation rather than operational controls currently exercised by the running application.
