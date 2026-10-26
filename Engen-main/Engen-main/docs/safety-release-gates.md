# Safety and startup release gates

This project is read-only/paper-only. There is no approved live execution path.

## Startup and emergency-stop behavior

- Configuration must validate before the paper monitor creates its SQLite stores or begins scanning.
- If `ENGINE_MODE` is not `read_only`, or `LIVE_TRADING_ENABLED` is not an explicit off value, startup fails closed.
- A configuration error does not dump the environment or echo arbitrary supplied values.
- If startup state is uncertain, do not continue to monitoring; stop and inspect the sanitized error. No automatic fallback to a more permissive mode is allowed.
- The current emergency-stop policy is fail-closed: all live execution remains unavailable. A future execution adapter must add a persistent emergency-stop latch before any signing or broadcast implementation is considered.

## Read-only and paper-trading boundary

- Public RPC calls are limited to data reads.
- `PaperTrader` updates virtual balances and paper history only.
- Paper mode must never call transaction signing or broadcast APIs.
- Wallet private keys, seed phrases, and live-trading credentials are not required for startup and must not be requested.

## Mandatory gates before any future live-execution proposal

A separate review would be required for all of the following; passing today's paper-mode tests does not satisfy these gates:

1. Threat model and explicit authorization model.
2. Persistent emergency-stop latch that defaults engaged after uncertain startup, restart, or configuration failure.
3. Independent execution adapter separated from monitoring and paper simulation.
4. Explicit allowlist and per-wallet/per-token risk limits.
5. Balance, slippage, price freshness, transaction simulation, and fee checks.
6. Idempotency and duplicate-broadcast handling.
7. Signing-key custody review; no secrets in browser storage, logs, or repository files.
8. Tests proving disabled mode cannot sign or broadcast, including injected failure paths.
9. Clean shutdown, restart, and interrupted-startup recovery tests.
10. Security review, deployment review, and a separate explicit user approval.

## Shutdown

The dashboard asks its monitor worker to stop and waits up to 15 seconds for exit. If the worker does not exit in that window, it emits a warning instead of claiming a clean worker shutdown. Long-running scan functions must remain bounded by RPC timeouts.

## Configuration safety

Configuration validation happens before monitoring startup. Error messages describe the setting to correct but do not print arbitrary rejected values. Never log the complete process environment or configuration object.
