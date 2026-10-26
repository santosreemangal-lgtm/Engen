# Engen — Solana Copy-Trading Research Engine

Engen is a **read-only monitoring and paper-trading research project** for public Solana wallet activity. The dashboard helps users maintain public wallet watchlists, inspect monitoring and RPC health, and review saved profile or paper-simulation data.

> **Safety boundary:** this project is not a live trading service. Do not add wallet seed phrases or private keys to the dashboard, repository, environment files, or issue reports. The dashboard must not sign or broadcast transactions. Paper simulation is not evidence of real-world profitability.

## Current capabilities

- Local web dashboard for public wallet watchlists and monitoring controls.
- Read-only Solana RPC health and public transaction observation.
- Profile storage and profile transaction history where the required local database is available.
- Paper-simulation reporting from an existing simulation database; reporting reads that database in read-only mode.
- Python unit tests and an offline mock-analysis check.

Availability depends on the checked-out branch and its configured local data. A missing database or unverified market-price source can leave simulation balances or unrealized P&L unavailable; do not interpret unavailable data as zero profit or loss.

## Run locally

Use Python 3.10 or newer. From the repository root:

```bash
python -m pip install -r Engen-main/Engen-main/requirements.txt
python Engen-main/Engen-main/main.py --help
```

The dashboard backend lives at `Engen-main/Engen-main/dashboard/server.py`. Run it from the project directory so local imports and relative paths resolve correctly:

```bash
cd Engen-main/Engen-main
python -m dashboard.server
```

By default, the server binds to `127.0.0.1:8000`. It has **no user authentication**. Do not expose it to the public internet or an untrusted network; use only on localhost or behind a trusted, authenticated deployment layer. Monitoring is opt-in and should be stopped when no longer needed.

## Dashboard API overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/wallets` | Read saved public wallet addresses |
| POST | `/api/wallets` | Add or remove a public wallet address |
| GET | `/api/monitor` | Read monitoring status |
| POST | `/api/monitor` | Start or stop read-only monitoring |
| GET | `/api/rpc-health` | Read public RPC health information |
| GET | `/api/profiles` | Read saved profiles |
| POST | `/api/profiles` | Create/update/delete profiles and settings |
| GET | `/api/profile-transactions` | Read paginated profile transaction history |
| GET | `/api/paper-simulation` | Read paper-simulation reports |

API writes change local watchlist/profile data or monitoring state; they do not authorize live trading. The server validates public Solana addresses and caps JSON request body size. Browser security headers are intended as defense in depth, not a substitute for authentication or a safe deployment boundary.

## Test and validate

From the repository root:

```bash
python -m unittest discover -s Engen-main/Engen-main/engine/engine/engine/tests -v
python -m compileall -q Engen-main/Engen-main
python main.py --mock-analysis-test
```

Tests should run without a funded wallet and must not require signing or broadcasting a transaction. Review CI results on the pull request before merging.

## Known limitations

- The local dashboard backend does not provide user authentication or multi-user authorization.
- Unrealized P&L requires an independently verified, sufficiently fresh market-price source; it must remain explicitly unavailable when that condition is not met.
- Deployment-specific backup/restore, HTTPS, secret management, and access control require environment-specific rehearsal.
- A green test suite cannot prove profitability, RPC uptime, or production security.

## Change policy

Keep changes small and reviewable. Preserve existing working functionality, keep live trading disabled, never commit secrets, and do not merge a change until the relevant tests and deployment assumptions have been checked.
