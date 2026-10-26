# Configuration and environment separation

## Current behavior

- `engine/config.py` loads and validates `ENGINE_MODE`, `LOG_LEVEL`, and `LIVE_TRADING_ENABLED`.
- Safe engine defaults are `ENGINE_MODE=read_only`, `LOG_LEVEL=INFO`, and live trading disabled.
- The paper-monitor CLI reads its runtime parameters from command-line options; its defaults and required arguments are defined in `engine/paper_monitor_runner.py`.
- The dashboard reads `DASHBOARD_HOST` from the process environment and defaults to `127.0.0.1`. Its port is currently fixed at 8000.
- `.env.example` is documentation only. The application does not automatically parse or load it.

## Development, testing, and production

- **Development/Codespaces:** use non-secret defaults. Keep dashboard binding on loopback unless an authenticated, intentionally configured access path is in place.
- **Tests:** pass configuration dictionaries to `load_config` or patch the test process environment. Tests must not depend on a developer's real `.env` file or real trading credentials.
- **Production:** inject configuration through the deployment environment or a secrets manager. Do not commit production environment files. Keep the dashboard inaccessible from untrusted networks until authentication, authorization, and transport protections are implemented and tested.

These are operating-environment boundaries, not a claim that separate deployment profiles or automatic `.env` loading have already been implemented. Do not add secret values to this example file.

## Configuration rules

- Unsupported `ENGINE_MODE` values are rejected; the current foundation supports `read_only` only.
- Unsupported log levels are rejected.
- Any truthy/unknown `LIVE_TRADING_ENABLED` value is rejected. Live trading is not implemented.
- The public RPC endpoint used by the paper runner is selected with `--rpc-url`; do not assume `SOLANA_RPC_URL` changes the runner's endpoint.
- Never print the full environment or include credentialed RPC URLs in errors.

## Public RPC retry and pacing controls

The shared read-only RPC transport reads these optional process environment variables:

- `ENGEN_RPC_RETRIES`: additional attempts after the first request; default `2`. Use a non-negative integer.
- `ENGEN_RPC_BACKOFF_SECONDS`: exponential backoff base in seconds; default `0.5`. The wait grows by attempt (base, 2×base, ...).
- `ENGEN_RPC_MIN_INTERVAL_SECONDS`: minimum process-wide gap between outbound requests; default `0.1` seconds.

Invalid or negative values fall back to the safe defaults. HTTP 429 and 5xx responses, plus transport/network failures, are retried within the configured bound. Invalid JSON responses and non-retryable HTTP errors are not retried. Error messages intentionally omit the configured RPC URL. These controls reduce bursts but do not replace an RPC provider's published quota or guarantee availability.

`.env.example` remains a reference only and is not automatically loaded. Set these values in the process/Codespaces environment to override defaults.

## Dashboard input bounds

The dashboard backend validates saved wallet addresses as Base58 public keys decoding to exactly 32 bytes, rather than relying only on character count. The saved-wallet list is capped at 100 entries, and JSON request bodies are capped at 10,000 bytes. These bounds reduce accidental resource growth; they do not provide authentication. Keep the dashboard bound to loopback unless it is placed behind a properly authenticated and protected deployment.
