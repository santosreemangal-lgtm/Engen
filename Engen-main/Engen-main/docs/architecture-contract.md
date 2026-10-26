# Architecture and API contract

Status: design contract for the existing read-only monitor and future private dashboard. This document does not claim that the future profile/API features have already been implemented.

## 045. Application-wide configuration versus profile settings

Application-wide settings belong to process/deployment configuration and apply consistently to the service:
- RPC endpoint, request timeout, retry/backoff limits, and request pacing.
- Log level and log redaction rules.
- Dashboard bind host and port.
- Global emergency-stop state.
- Environment/deployment mode and global safety policy.

Profile settings are stored per destination profile and must not override global safety policy:
- Enabled/disabled state, source-wallet associations, destination public address, quote mint, virtual starting balance, scan interval, and per-profile paper-simulation preferences.

A profile cannot enable live trading or override the global read-only/paper-only boundary. Secrets never belong in frontend configuration or profile JSON.

## 046. Read-only versus authorized execution boundary

Read-only components may fetch public RPC data, parse transaction metadata, and persist observations. The paper simulator may update only virtual balances and simulated trade history. No current monitoring or dashboard route may sign or broadcast transactions. Any future execution adapter must be a separate component, default disabled, and gated by a separately reviewed authorization and readiness process. Tests must stub/block all broadcast paths.

## 047–048. Frontend/backend API contract

Current routes are `GET /api/wallets`, `POST /api/wallets`, `GET /api/monitor`, and `POST /api/monitor`. The current API is small and does not yet implement profiles or authentication. Preserve these routes during migration until compatibility tests exist.

Target success envelope:
```json
{
  "ok": true,
  "data": {},
  "error": null,
  "request_id": "opaque-request-id"
}
```

Target error envelope:
```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "The request could not be validated."
  },
  "request_id": "opaque-request-id"
}
```

Do not return stack traces, credentials, full environment variables, or internal filesystem paths. Use suitable HTTP status codes: 400 for invalid input, 401 for unauthenticated requests, 403 for forbidden actions, 404 for unknown routes/resources, 409 for conflicts, 413 for oversized bodies, 429 for rate limiting, and 500 for unexpected server failures. The envelope is a target contract; changing the existing responses requires frontend/backend integration tests.

## 049. Time and timezone contract

Persist event and observation timestamps in UTC using ISO 8601 with an explicit `Z` or `+00:00` offset. Preserve chain-provided Unix timestamps separately when available. Convert to the user's local timezone only for display. Missing timestamps remain null/unknown; do not synthesize an on-chain time from the server clock.

## 050. Amount precision

Keep raw integer token amounts and token decimals wherever RPC data supplies them. Use Python `Decimal` or integer base units for calculations; do not use binary floating-point for balances, paper fills, or P&L. Format values only at the presentation boundary and do not round stored quantities. Missing decimals or malformed amounts must be represented as unavailable, not guessed.

## 051–052. Mint and SOL representation

Validate Solana public-key syntax and, where possible, decode Base58 and require exactly 32 bytes. Do not treat a length-only regular expression as complete validation. Store token mint addresses exactly as canonical public-key strings.

Represent native SOL separately from SPL-token balances. Wrapped SOL is an SPL token with its own mint and must not be counted as an additional independent asset on top of the same underlying SOL movement. Preserve token-account owner and instruction context when available; classify wrap/unwrap, transfers, fees, rent, and swaps separately where evidence supports it. Ambiguous activity remains UNKNOWN.

## 053. History and pagination

History endpoints must use bounded pagination with a stable ordering and opaque cursor or validated limit/offset. Set a conservative default page size of 50 and a hard maximum of 200 until load testing justifies a different value. Never load unbounded history into a dashboard response. Retention policy must be configurable/documented before deleting historical records; no history should be deleted implicitly when a profile is edited.

## 054. Initial threat model

Assets: public wallet history, profile configuration, virtual portfolio data, local databases, dashboard control endpoints, and any future authorization credentials. Threats include unauthorized remote access, cross-profile data leakage, malformed RPC/API responses, request flooding, path/URL injection, secret leakage through logs, duplicate processing, stale or misleading price data, and accidental live execution. Public addresses are not secret, but profile associations and portfolio history may be private. Default dashboard binding must remain loopback. Any external exposure requires authentication, authorization, TLS, CSRF/origin protections where applicable, and rate/request-size limits.

## 055–056. Expected users and clients

Initial scale target: approximately 1–5 trusted users. Prefer a simple, maintainable single-service design with SQLite for local persistence until measured concurrency or deployment needs justify a change. Support current evergreen desktop and mobile browsers; layouts must remain usable at narrow mobile widths (320 CSS pixels and above), tablet widths, and desktop. Do not promise unsupported browsers without tests.

## 057. Health states

- `stopped`: monitoring is not running.
- `starting`: the worker has been requested but has not completed a scan.
- `healthy`: at least one scan succeeded and data is within the configured freshness window.
- `degraded`: a scan failed or data is stale, but recovery may be possible.
- `error`: initialization/configuration failed or monitoring cannot continue.
- `unknown`: there is insufficient evidence to determine health.

A timestamp must accompany last-successful-scan status. A successful HTTP response alone does not prove the monitored data is fresh.

## 058. Subsystem acceptance criteria

- Configuration: defaults are safe; invalid/conflicting values fail closed; secrets are not logged.
- RPC: response shapes are validated; timeouts, rate limits, missing metadata, and provider errors are bounded and explicit.
- Monitoring: per-wallet state is isolated; restart does not lose the deduplication position; retries are bounded; stop is clean.
- Classification: buy/sell require transaction context plus consistent balance evidence; fees/transfers/rent are not silently treated as trades; uncertain cases remain UNKNOWN.
- Storage: stable IDs and uniqueness constraints prevent duplicate effects; updates are transactional; restarts preserve state.
- Paper trading: virtual balances are exact, reproducible, and clearly separate from observed real balances; no broadcast function is called.
- Dashboard/API: schema validation, authentication before private remote exposure, useful error states, mobile/keyboard usability, and frontend/backend contract tests.
- Release: complete test suite, startup/shutdown/restart checks, dependency review, documentation, and a security review all pass while live trading remains disabled.

## 059–060. Architecture decisions and implementation sequence

1. Preserve the existing Python engine, SQLite-backed signature/paper stores, and HTML dashboard; do not replace them wholesale.
2. Keep public RPC access read-only. Keep simulation separate from monitoring and from any future execution adapter.
3. Implement profile/settings schemas and validation before profile UI, then persistence and isolation tests.
4. Stabilize the API contract with compatibility tests before changing frontend response handling.
5. Add authentication and authorization before exposing private dashboard data beyond loopback.
6. Add price/portfolio valuation only with explicit source, timestamp, stale-data behavior, and reproducible calculation tests.
7. Run all checks in GitHub Actions and the intended Codespaces/deployment environment; CI success alone does not prove deployment readiness.
8. Keep live trading disabled. A future live-trading proposal requires a separate threat model, readiness report, and explicit approval; it is outside this implementation plan.

No major refactor is authorized by this document alone. Implement each step incrementally and review its tests before proceeding.
