# Engen — Rust-native Solana Monitor and Paper Simulator

Engen monitors public Solana wallet activity and supports virtual paper execution only. The backend migration is in progress: Rust is the target runtime, but the migration PRs remain Draft until behavioral parity, data compatibility, restart recovery, and deployment gates are accepted.

## Safety boundary

- Public Solana RPC reads only.
- Balances, orders, fills, and P&L are virtual simulations; no real trade is submitted.
- No private-key or seed-phrase ingestion, storage, or logging.
- No transaction signing, live transaction construction, transaction broadcast, or live-mode switch.
- Monitoring is opt-in; starting the dashboard does not automatically start a wallet scan.
- Never run migration tests against user data databases. Tests must use temporary SQLite files.
- Do not merge the migration PRs or cut over a deployment until every acceptance gate passes.

## Migration status

The Rust candidate includes a native dashboard HTTP server, paper-copy execution, wallet monitoring, transaction processing, profile/signature stores, and a Rust CLI. The dashboard also exposes the pending_copy report field and loads its UI helper from the Rust server.

The migration is **not complete or approved for cutover**. Remaining acceptance areas include full Python/Rust differential coverage, cross-process monitor claims and crash recovery, Telegram parity or an explicit retirement decision, health/logging and operations-tool parity, whole-application database compatibility and restart tests, full E2E tests, and production deployment verification. Python files are retained as behavioral references until these gates pass.

Review the Draft migration PRs before any merge:
- [PR #48 — canonical Rust backend migration](https://github.com/santosreemangal-lgtm/Engen/pull/48)
- [PR #49 — Rust dashboard runtime](https://github.com/santosreemangal-lgtm/Engen/pull/49)

## Run the Rust candidate locally

From the repository root:

~~~bash
cargo build --release --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml
bash scripts/run_engen_rust_dashboard.sh
~~~

The candidate dashboard uses port 8000 by default. Open it only on localhost or through a trusted, authenticated Codespaces port-forwarding environment. The hosted Render candidate is configured in render.yaml; it uses the Rust runtime and launcher.

The launcher sets ENGEN_APP_ROOT and invokes the compiled engen-engine serve-dashboard binary. It does not start Python. Do not expose the backend directly to an untrusted network. For remote access, use a trusted HTTPS reverse proxy and configure a strong DASHBOARD_AUTH_TOKEN.

## Rust CLI and checks

~~~bash
cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- check-config
cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- health-check
cargo run --manifest-path Engen-main/Engen-main/rust-engine/Cargo.toml -- --help
~~~

The native health check validates Rust runtime files, read-only configuration, and the unconditional live-execution block. It does not access user ledgers or make RPC requests.

## Rust verification

From the repository root:

~~~bash
cd Engen-main/Engen-main/rust-engine
cargo fmt --all -- --check
cargo test --all-targets
cargo clippy --all-targets -- -D warnings
~~~

The Rust workflow also runs the localhost dashboard process E2E, a read-only public RPC smoke test, and selected Python behavioral-reference tests. A passing CI run is evidence for that exact commit only; it is not by itself authorization to cut over.

## Python behavioral-reference tests (not production launchers)

Python is retained temporarily to compare legacy behavior with Rust. These commands run reference tests only; do not use legacy Python entry points to launch the production candidate.

~~~bash
cd Engen-main/Engen-main
PYTHONPATH=. python -m unittest discover -s engine/engine/engine/tests -v
~~~

Do not remove or move Python reference modules until their Rust equivalents have matching acceptance tests and the final migration audit passes.

## Configuration

The Rust runtime defaults to ENGINE_MODE=read_only and requires LIVE_TRADING_ENABLED to be absent or false. Public RPC configuration uses ENGEN_RPC_URL (HTTPS for remote endpoints), with bounded timeout, retry, and pacing settings. Paper-only fee settings are ENGEN_PAPER_TRADING_FEE_BPS, ENGEN_PAPER_NETWORK_FEE_USD, and ENGEN_PAPER_PRIORITY_FEE_USD.

These are simulated accounting settings, not live network fee quotes. Do not commit RPC provider credentials or dashboard tokens.

## Data preservation

Never delete, rewrite, or automatically migrate the user's existing data/ SQLite databases as part of the code migration. Validate compatibility using disposable database copies first. Preserve audit history, old order references, and source transaction evidence. Keep backups until a separately reviewed backup/restore test proves they are recoverable.
