# Engen — Solana Wallet Monitoring and Paper Simulation

Engen is a read-only monitoring and research project for public Solana wallet activity. The dashboard provides public-wallet watchlists, monitoring status, RPC health, destination-profile management, transaction-history filters, and paper-simulation reporting.

> **Safety boundary:** This repository must not store wallet seed phrases or private keys, sign transactions, or broadcast trades. Paper-trading outputs are virtual and are not real wallet balances or evidence of profitability.

## Dashboard features

- **Engine overview:** read-only mode, monitor status, last successful scan, and RPC health.
- **Wallet watchlist:** add/remove public addresses; address input is validated and the saved list is bounded.
- **Destination profiles:** manage public destination addresses, source-wallet associations, and profile-specific settings.
- **Transaction history:** paginated records with classification, status, and token-mint filters.
- **Paper simulation:** separate simulated balances, orders, fills, and realized P&L; the report reads its database without creating or mutating it.
- **Failure handling:** backend/API failures are surfaced to the UI; no stale transaction-history results should be treated as fresh after a failed request.

## Run locally

Use Python 3.10 or newer. From the repository root:

```bash
python -m pip install -r Engen-main/Engen-main/requirements.txt
cd Engen-main/Engen-main
python -m dashboard.server
```

The backend defaults to `127.0.0.1:8000` and refuses to bind to non-loopback interfaces. Do not change this boundary to expose the backend directly to a network. For remote access, keep the backend bound to localhost behind a trusted reverse proxy that enforces HTTPS and user authentication.

An optional API bearer token can be configured with the `DASHBOARD_AUTH_TOKEN` environment variable (at least 32 characters). When configured, every `/api/*` request requires `Authorization: Bearer <token>`; the dashboard's token field keeps the value in the current browser tab's session storage. Use a randomly generated secret, never commit it, and clear it when finished. If the variable is unset, the API is intended only for localhost use. This is a single-user dashboard, not a multi-user authorization system: do not treat a shared token as per-profile access control.

State-changing browser requests with a mismatched `Origin` are rejected. Request bodies are capped at 10,000 bytes, write requests must use JSON, and the API applies an in-memory limit of 120 requests per client IP per 60 seconds. This rate limiter is process-local; deployments with multiple workers or reverse proxies need a proxy/shared-store limit and load testing. These controls are defense in depth, not a substitute for a properly configured authenticated HTTPS proxy when remote access is required. Monitoring is opt-in; check status before starting and stop it when finished.

## API reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/wallets` | Read saved public wallet addresses |
| POST | `/api/wallets` | Add/remove a public address |
| GET | `/api/monitor` | Read monitor status |
| POST | `/api/monitor` | Start/stop read-only monitoring |
| GET | `/api/rpc-health` | Read public RPC health |
| GET | `/api/profiles` | Read destination profiles |
| POST | `/api/profiles` | Create/update/delete profiles, settings, and source-wallet associations |
| GET | `/api/profile-transactions` | Read filtered, paginated transaction history |
| GET | `/api/paper-simulation` | Read the paper-simulation report |

API write operations update local profile/watchlist data or monitor state. They do not enable live trading. JSON request bodies are bounded; public addresses and profile settings are validated by the backend. Browser security headers, optional bearer authentication, same-origin write checks, and loopback-only binding provide defense in depth. Per-user/per-profile authorization is not implemented; use this as a single-user local dashboard only.

## Validation

Run from the repository root:

```bash
python -m compileall -q Engen-main/Engen-main
python -m unittest discover -s Engen-main/Engen-main/engine/engine/engine/tests -v
python main.py --mock-analysis-test
```

The GitHub Actions workflow checks compilation, offline unit tests, and the mock-analysis command on pull requests. Confirm that its run succeeds before merging.

## Important limitations

- The dashboard backend does not implement user authentication or multi-user authorization.
- Unrealized P&L must remain unavailable unless a verified, fresh market-price source exists.
- Transaction fee fields must not be fabricated when the stored schema does not supply verified fee data.
- Backup/restore, HTTPS, secret management, and access control need deployment-specific testing.
- Passing unit tests does not establish profitability or production security.

## Change policy

Preserve working functionality and use small reviewable changes. Do not commit secrets. Keep monitoring read-only and live trading disabled. Do not merge until the relevant checks and deployment assumptions are verified.
