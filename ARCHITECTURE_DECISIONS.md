# Architecture Decisions and Acceptance Criteria

**Status:** Requirements baseline for the current read-only / paper-simulation project. This document is not a claim that every listed capability is implemented or production-certified.

## Boundaries and data flow

- **Engine:** fetches public Solana RPC data, validates RPC envelopes, monitors public source wallets, classifies observed transactions, stores evidence, and calculates paper-simulation results.
- **Dashboard:** presents backend state and validated records; submits public-address/profile changes and monitor start/stop requests. It is not an execution authority.
- **Read-only boundary:** no seed phrase/private-key storage, transaction signing, or transaction broadcast. Paper simulation is virtual and must never be represented as a real execution.
- **Flow:** configured RPC endpoint → bounded RPC transport → signature/transaction validation → monitoring/deduplication → classification with evidence and explicit UNKNOWN outcomes → SQLite persistence → read-only dashboard API → frontend validation/rendering.
- **Source wallets and destination profiles:** source wallets are public observation targets. A source wallet may be associated with multiple profiles; associations are stored separately. Destination addresses are public addresses, never signing material. New profiles are disabled by default.
- **Identity:** stable profile IDs are UUIDs; source wallet IDs are their validated public addresses; transaction IDs are signatures with a database uniqueness constraint.

## Data and numeric conventions

- Store timestamps as ISO-8601 with an explicit timezone; emit UTC timestamps in new records. UI may localize only for display.
- Preserve raw on-chain quantities as integer decimal strings plus token decimals. Do not convert raw amounts to binary floating-point for accounting.
- Validate public addresses as base58 encodings of 32-byte values. Validate transaction signatures as base58 encodings of 64-byte values before RPC lookup.
- Classifications include BUY_CANDIDATE, SELL_CANDIDATE, TRANSFER, UNKNOWN, FAILED_TRANSACTION, RENT, and LIQUIDITY_OPERATION. A token receipt alone or SOL decrease alone is not sufficient evidence of a buy/sell.
- Transaction statuses distinguish observed, confirmed, failed, pending, and unknown. Missing provider metadata remains explicit rather than fabricated.
- Portfolio accounting uses Decimal, average cost basis, and USD as the currently supported valuation currency. Price data requires a source and observation timestamp; stale or missing prices must make valuation unavailable, not zero.
- Transaction fee data is shown only when verified. Deposits, withdrawals, internal transfers, and airdrops must not be treated as trading profits.

## Profile settings and application configuration

- Application-wide environment settings include engine mode, log level, live-trading disable flag, RPC endpoint/retry/pacing controls, dashboard bind host, optional dashboard bearer token, and optional database paths.
- Profile settings are persisted separately per profile. Current settings storage validates JSON-safe values and rejects secret-like authorization fields; it does not yet enforce a comprehensive typed schema for all profile options.
- Safe defaults: ENGINE_MODE=read_only, LIVE_TRADING_ENABLED=false, dashboard bind 127.0.0.1, profile status disabled, monitoring starts only after an explicit UI action.
- RPC endpoint must use HTTPS for remote hosts. Plain HTTP is allowed only for loopback testing. Credentials must not be embedded in committed files or logged.
- Invalid RPC retry/pacing values fail clearly; retry count is bounded. RPC timeouts must be finite and in the range greater than 0 through 60 seconds.

## API contract

- Existing routes are listed in the README API reference. JSON write requests require Content-Type: application/json, a bounded body, and a JSON object.
- Successful responses retain route-specific shapes for backward compatibility. Errors use an error string and an appropriate HTTP status. Clients must validate expected fields and must treat non-2xx responses as failures.
- State-changing browser requests reject a mismatched Origin. Optional shared bearer authentication is not per-user or per-profile authorization.
- Transaction history must remain filtered and paginated with bounded page sizes. Stable unique signatures prevent duplicate transaction records.
- Frontend state must not retain stale history as fresh after a failed fetch. Simulated orders/fills and paper P&L must be labeled as simulation.

## Threat model and deployment

- Primary risks: malicious browser-origin writes, unauthorized local/network API access, oversized or malformed requests, rate abuse, secret leakage in logs/config, malicious RPC responses, duplicate processing, stale market data, and accidental transition into live execution.
- Current supported use is one trusted user on localhost. Remote deployment is not approved by this document. It requires a trusted HTTPS reverse proxy, authentication, proxy-aware rate limiting, and deployment-specific security tests while the backend stays private on loopback.
- A shared bearer token does not isolate users or profiles. Do not use this backend as a multi-user service until identity and ownership checks are implemented.
- No secret phrases or private keys are accepted. Any future signed transaction path needs a separate reviewed design and explicit approval for the exact commit; this project currently does not implement it.

## Acceptance criteria

1. Offline tests pass for the affected code and the complete test suite.
2. Compilation passes and the mock-analysis health check passes.
3. Malformed/invalid RPC envelopes, network failures, and unsupported transport configurations fail safely without exposing endpoint credentials.
4. Duplicate transaction signatures and paper signals do not duplicate persisted effects.
5. Unknown classification and unavailable/stale price data are represented honestly.
6. Profile settings remain isolated, profile deletion preserves history, and backup/restore is tested.
7. Dashboard write endpoints validate inputs, reject mismatched browser origins, bound request sizes, and remain loopback-only.
8. No change enables signing, transaction broadcast, or live trading.
9. A release is not production-ready until external deployment controls, access-control requirements, restart recovery, and a broader security review have been independently verified.

## Open decisions / not yet certified

- Comprehensive typed profile-settings schema and documented per-setting defaults.
- Multi-user identity and per-profile ownership/authorization model.
- Production HTTPS/reverse-proxy deployment and load testing.
- Full process crash/restart rehearsal with active monitoring and persistent simulation state.
- Independent dependency vulnerability scan and external security review.
- No live-mode activation decision is made; see LIVE_MODE_SAFETY_GATE.md.
