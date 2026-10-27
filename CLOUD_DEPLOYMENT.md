# Persistent cloud deployment (paper trading only)

This repository includes a Render Blueprint in `render.yaml`. It configures the dashboard backend as a long-running web service, keeps its backend bound behind Render's HTTPS proxy, requires a generated bearer token for API calls, and mounts durable storage for SQLite databases, wallet watchlists, and explicit monitor opt-ins.

## Deploy from the GitHub UI

1. Sign in to Render and choose **New → Blueprint**.
2. Connect the GitHub repository `santosreemangal-lgtm/Engen` and select branch `feature/paper-trading-dashboard-integration`.
3. Review `render.yaml` before creating the service. The `starter` web service and persistent disk may incur charges; do not create it until you approve the displayed price.
4. Create the service. Render should generate `DASHBOARD_AUTH_TOKEN`; do not put this value in GitHub or share it in chat.
5. When deployment is healthy, open the service URL over HTTPS. Copy the generated token from the service's private environment settings and enter it in the dashboard's API-token field for the current browser session.
6. Create or activate a profile, add the source trader's **public** wallet address, choose Buy only / Sell only / Buy and sell, then explicitly start monitoring.
7. Confirm the monitor reports enabled and that a new eligible, confirmed public-wallet swap produces a paper order. Keep the service logs and dashboard status visible during the test.

## What persists

- Watchlist wallet addresses and profile data use paths under `/var/data`.
- The app records explicit monitor start/stop choices in `monitoring_state.json`. After a service restart, it resumes only monitors that were previously started, whose profiles still exist, are active, and still have source wallets.
- Stopping monitoring removes that opt-in. An active profile alone does not silently start monitoring.

## Security and limits

- The service uses a generated API bearer token and expects the hosting provider's HTTPS reverse proxy. The backend refuses non-localhost binding unless `DASHBOARD_TRUST_PROXY=true` and a token of at least 32 characters are configured.
- Never add a wallet seed phrase or private key. This deployment is for public-wallet monitoring and virtual paper orders only; it does not sign or broadcast real transactions.
- Persistent storage and a running service are required for dependable monitoring. A hosting plan can still restart or experience outages, and this is not a guarantee of uninterrupted 24/7 availability.
- The dashboard's monitoring worker is process-local. Do not scale the service to multiple instances unless monitor ownership/locking is redesigned to prevent duplicate polling and orders.
- Verify the GitHub Actions test run and complete a real cloud smoke test before relying on it. Passing unit tests alone does not prove the hosted service works end-to-end.
