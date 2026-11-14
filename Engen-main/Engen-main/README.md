# Engen — Rust Wallet Monitoring and Paper Simulation

This project monitors public Solana wallet activity and supports virtual paper execution. Rust is the target backend runtime. The migration remains under Draft review; production cutover is not approved until every API, behavioral, persistence, recovery, security, and deployment acceptance gate has passed.

> **Safety boundary:** Public RPC reads only. No private-key or seed-phrase handling, signing, live transaction construction, or transaction broadcast exists. All orders, balances, fills, and P&L are virtual.

## Rust dashboard candidate

Build and start from the repository root:

~~~bash
cargo build --release --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml
bash scripts/run_engen_rust_dashboard.sh
~~~

The launcher invokes the Rust engen-engine serve-dashboard command and never starts Python. The dashboard uses port 8000 by default. Keep it on localhost or behind a trusted HTTPS reverse proxy with authentication. Do not expose a direct unauthenticated backend.

The candidate Render service is described in the repository-root render.yaml. It uses the Rust runtime, a persistent disk, and the Rust launcher. Deployment configuration in a Draft branch is not a production cutover.

## CLI and health checks

~~~bash
cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- check-config
cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- health-check
cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- --help
~~~

The native health check confirms required Rust runtime files exist, engine mode is read_only, live trading is disabled, and the live-execution guard always rejects execution.

## Dashboard API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | /api/wallets | Read saved public wallet addresses |
| POST | /api/wallets | Add/remove a public address |
| GET | /api/monitor | Read global monitor status |
| POST | /api/monitor | Start/stop read-only monitoring |
| GET | /api/rpc-health | Read public RPC health |
| GET | /api/profiles | Read destination profiles |
| POST | /api/profiles | Create/update/delete profiles, settings, and source-wallet associations |
| GET | /api/profile-transactions | Read filtered, paginated transaction history |
| GET | /api/profile-monitor?profile_id=<UUID> | Read one profile's monitor status |
| POST | /api/profile-monitor | Start/stop read-only monitoring for one active profile |
| GET | /api/paper-simulation | Read global or profile-scoped virtual-ledger report |

Paper-simulation reports include the pending_copy summary (pending_count, up to 20 pending orders, and explicit false live/broadcast flags). The dashboard loads /dashboard/pending_copy_ui.js to display that count.

API writes update local watchlists, profile metadata, monitor state, or virtual paper state only. They cannot enable live trading. Profile history, monitor lifecycle, and paper-ledger requests are scoped by canonical profile UUID. Missing or stale verified prices must remain unavailable rather than being fabricated.

## Configuration and security

The application reads environment variables directly; it does not automatically load a .env file. Important settings include:

- ENGINE_MODE=read_only
- LIVE_TRADING_ENABLED=false
- ENGEN_RPC_URL — optional HTTPS public Solana RPC endpoint
- DASHBOARD_HOST and PORT
- DASHBOARD_AUTH_TOKEN — optional bearer token, at least 32 characters
- DASHBOARD_TRUST_PROXY — only when running behind the configured trusted proxy
- SIGNATURE_DB, PROFILE_STORE_DB, PAPER_SIMULATOR_DB, PROFILE_DATA_DIR, WALLETS_FILE, and MONITOR_STATE_FILE

Never commit provider credentials or dashboard tokens. The dashboard uses bounded request bodies, same-origin write checks, security headers, and an in-memory API rate limit. The in-memory limit is not a substitute for a shared/proxy rate limit in a multi-worker deployment.

Paper fee configuration is virtual accounting only:

- ENGEN_PAPER_TRADING_FEE_BPS (default 10; range 0–10,000)
- ENGEN_PAPER_NETWORK_FEE_USD (default 0)
- ENGEN_PAPER_PRIORITY_FEE_USD (default 0)

The network and priority fee fields are fixed USD estimates per simulated fill; they are not live Solana fee quotes. Profile overrides are stored with the simulated order/fill so a retry does not silently change its fee model.

## Rust verification

Run from Engen-main/Engen-main/rust-engine:

~~~bash
cargo fmt --all -- --check
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
~~~

The GitHub workflow also performs a read-only public RPC smoke test, selected Python behavioral-reference tests, browser JavaScript syntax checks, and a localhost dashboard process E2E.

## Optional Telegram paper bot

The Rust CLI includes the optional allowlisted Telegram paper-bot process:

~~~bash
TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_CHAT_IDS=12345 \
  cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- serve-telegram-paper-bot
~~~

The command requires both environment values and ignores chats outside the allowlist. It is not started by the Dashboard or configured as a hosted worker. Its paper ledger is a separate in-memory portfolio, not shared with the dashboard's persistent paper simulator. Never commit a Telegram bot token. This adapter supports virtual-only commands and does not expose live trading, signing, or transaction broadcasting.

## Legacy Python behavioral references

Python source files and tests are retained temporarily to compare behavior while the migration remains open. They are not the documented production launchers. Run only the reference tests from the canonical application directory:

~~~bash
cd Engen-main/Engen-main
PYTHONPATH=. python -m unittest discover -s engine/engine/engine/tests -v
~~~

Outstanding migration work includes full Python/Rust output parity, cross-process monitor claims and crash recovery, Telegram adapter parity or a reviewed retirement decision, remaining operational utility ports, full application restart/data-compatibility E2E, latency measurements, and final Python-free deployment verification. Keep the migration PRs Draft; do not remove Python reference files or cut over the user's existing runtime before the acceptance matrix is green.

## Data safety and rollback

Migration tests must use temporary SQLite databases. Do not open, migrate, rewrite, prune, or delete the user's data/ databases as a side effect. Preserve audit records and source transaction evidence. A rollback plan must identify the prior deployment artifact and prove the database remains compatible before a Rust deployment can replace an existing runtime.

## Live execution

Live signing and broadcasting are not implemented or authorized. No environment variable or API action can turn them on in this release.
