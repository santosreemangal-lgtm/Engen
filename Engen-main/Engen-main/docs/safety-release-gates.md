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

## Checklist implementation map: Tasks 080–090

| Task | Implementation evidence |
|---|---|
| 080 — Emergency stop safe after uncertain startup | `engine/safety.py` provides a default-engaged latch; unsafe configuration aborts before storage initialization. |
| 081 — Read-only blocks signing | `block_live_execution()` fails closed; the current runtime exposes no authorized live execution mode. |
| 082 — Paper trading cannot broadcast | Regression test parses `engine/paper_trader.py` and rejects network/signing imports and broadcast/signing entry-point names. |
| 083 — Checks before future live trading | The mandatory release gates above are documented; live execution remains out of scope and disabled. |
| 084 — Interrupted-startup handling | Paper runner returns exit code 130 when storage initialization is interrupted, before constructing the trader or starting scans. |
| 085 — Clean shutdown | Dashboard shutdown waits up to 15 seconds for its monitor worker and warns if it remains alive. |
| 086 — Stop background tasks | `MonitorService.stop_and_wait()` requests stop and joins the worker with a bounded timeout; tests cover worker exit. |
| 087 — Avoid full environment/config logs | Configuration errors no longer echo rejected values; startup failure logs are generic and do not dump environment variables. |
| 088 — Conflicting-config checks | Paper runner validates the same safe configuration policy and rejects live-enabled or non-read-only settings before initialization. |
| 089 — Configuration tests | Unit tests cover defaults, rejected modes, live-trading flags, sanitized errors, and fail-closed startup. |
| 090 — Startup without live-trading credentials | Existing paper-runner startup tests use no signing key or seed phrase and assert `real_trading=false` and `wallet_signing=false`. |

These tasks are implemented within the current read-only/paper-only boundary. They do not constitute approval or readiness for live trading.
