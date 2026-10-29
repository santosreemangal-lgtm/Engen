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

## User-count scope and authorization decision (Task 055 / 273)

- Current supported scope: one trusted user on localhost. The checklist's “approximately 1–5 users” is a target to evaluate, not current verified capacity.
- Do not expose this application as a multi-user service. The optional shared bearer token authenticates possession of one shared secret; it does not establish user identity, profile ownership, or role-based permissions.
- The current single-user implementation now scopes monitored-trader associations, profile monitor lifecycles, transaction/balance history, and paper-trading databases to a canonical profile UUID. The paper-only runner refuses to start for a missing/disabled profile or a public wallet not associated with that profile.
- This is profile isolation for one trusted local user, not multi-user ownership authorization. A future 1–5-user release must define identity/session management, profile ownership, who can read/update/delete profiles and history, administrative roles if any, audit events, session revocation, and concurrency limits.
- Acceptance tests for any future multi-user release must prove that user A cannot read, update, delete, or enumerate user B's profile, settings, source-wallet associations, transaction history, or simulation records by guessing/changing identifiers. Every route must enforce authorization server-side.
- Task 060 approval is pending: this document is a proposal baseline, not evidence of owner approval. Record the approved revision and date only after explicit review.

## Threat model and deployment

- Primary risks: malicious browser-origin writes, unauthorized local/network API access, oversized or malformed requests, rate abuse, secret leakage in logs/config, malicious RPC responses, duplicate processing, stale market data, and accidental transition into live execution.
- Current supported use is one trusted user on localhost. Remote deployment is not approved by this document. It requires a trusted HTTPS reverse proxy, authentication, proxy-aware rate limiting, and deployment-specific security tests while the backend stays private on loopback.
- Profile-scoped monitoring and paper-ledger endpoints require a valid profile UUID and do not fall back to global simulation data when a profile ID is supplied. The single shared bearer token still does not isolate different human users or establish profile ownership; do not use this backend as a multi-user service until identity and ownership checks are implemented.
- No secret phrases or private keys are accepted. Any future signed transaction path needs a separate reviewed design and explicit approval for the exact commit; this project currently does not implement it.

## Acceptance criteria

1. Offline tests pass for the affected code and the complete test suite.
2. Compilation passes and the mock-analysis health check passes.
3. Malformed/invalid RPC envelopes, network failures, and unsupported transport configurations fail safely without exposing endpoint credentials.
4. Duplicate transaction signatures and paper signals do not duplicate persisted effects.
5. Unknown classification and unavailable/stale price data are represented honestly.
6. Profile settings remain isolated, profile deletion preserves history, and backup/restore is tested.
7. Each profile's monitor lifecycle and paper ledger are isolated; the paper-only runner applies supported per-profile position-size/open-position limits and refuses profiles that are disabled or do not own the monitored source-wallet association.
7. Dashboard write endpoints validate inputs, reject mismatched browser origins, bound request sizes, and remain loopback-only.
8. No change enables signing, transaction broadcast, or live trading.
9. A release is not production-ready until external deployment controls, access-control requirements, restart recovery, and a broader security review have been independently verified.

## Task-specific release gates (055, 059, 060, 069, 273, 294, 296, 298–300)

- 055: Decide whether this release remains single-user or must support 1–5 concurrent users. The current release remains single-user until identity and authorization tests pass.
- 059: Maintain this architecture decision record for consequential choices; distinguish accepted decisions from open questions.
- 060: Obtain explicit owner approval for the stable implementation plan; approval is not inferred from code edits or passing CI.
- 069: .env.example must contain placeholders only, and root .gitignore must exclude real environment files and secret material. A separate repository-history secret scan is still required to establish that credentials were not previously committed.
- 273: Do not mark complete until profile ownership is enforced on every relevant API route and negative cross-profile tests pass.
- 294: A unit test that reconstructs a store is not equivalent to a full process crash/restart rehearsal. Test restart with active monitoring and persisted paper-simulation data.
- 296: Record a dated dependency vulnerability scan and deployment-security review, including transitive dependencies and remediation.
- 298: Production readiness requires independent evidence for security, access control, restart recovery, integration, and deployment controls.
- 299: Live mode remains unimplemented and unauthorized. Any future change needs a separate review and explicit approval for the exact commit and environment.
- 300: The final report must cite the exact commit and successful current CI run, describe what was actually exercised, list unresolved risks, and state deployment status. Do not use “all 300 complete” while any mandatory gate remains open.

## Open decisions / not yet certified

- Comprehensive typed profile-settings schema and documented per-setting defaults beyond the supported paper_trading keys.
- Multi-user identity and per-profile ownership/authorization model; current profile isolation is for one trusted local user only.
- Production HTTPS/reverse-proxy deployment and load testing.
- Full process crash/restart rehearsal with active monitoring and persistent simulation state.
- Independent dependency vulnerability scan and external security review.
- No live-mode activation decision is made; see LIVE_MODE_SAFETY_GATE.md.
