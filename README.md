# Engen — Solana Wallet Monitor

Engen is an early-stage, **read-only Solana wallet monitor** intended for offline tests and paper-trading development only.

## Safety status

- Live trading must remain disabled; Stage 1 rejects attempts to enable it.
- Never put seed phrases, private keys, or wallet passwords in source code or chat.
- Monitoring is opt-in; starting the dashboard server does not automatically start a scan.
- The dashboard API has no user authentication. Run it only in a trusted development environment and do not expose it publicly.
- Paper-trading results are simulations, not evidence of real-world profitability.
- The paper portfolio is in-memory and resets when the dashboard process restarts. It is not connected to live prices or wallet balances.
- Telegram commands are implemented for the virtual paper portfolio only. Configure an authorized chat ID before running the bot; it does not mirror wallet transactions into trades.
- The demo paper ledger is currently in-memory and resets when the dashboard or bot process restarts.
- The dashboard currently has no authentication or CSRF protection. Keep it on localhost or behind trusted access controls.

## Repository layout

The canonical application is in `Engen-main/Engen-main/`. The repository-root `main.py` is a compatibility entry point. The test suite remains under `Engen-main/Engen-main/engine/engine/engine/tests/`. The audit branch removes two duplicate nested modules after matching their content against canonical modules and searching for references. Review CI before merging.

## Requirements

- CI uses Python 3.10.
- The offline test suite uses the Python standard library; no package installation is required for mock tests.
- Explicit public Solana RPC calls require network access.

## Run offline checks

From the repository root, compile the canonical project and run the mock analysis:

```bash
python -m compileall -q Engen-main/Engen-main
python Engen-main/Engen-main/main.py --mock-analysis-test
```

Run the unit tests from the canonical project directory so Python can resolve the `engine` package:

```bash
cd Engen-main/Engen-main
PYTHONPATH=. python -m unittest discover -s engine/engine/engine/tests -v
```

Test the root compatibility entry point from the canonical project directory:

```bash
python ../../main.py --mock-analysis-test
```

## Start today's manual paper-trading demo

From the repository root, run:

```bash
cd Engen-main/Engen-main
python -m dashboard.server
```

Open the forwarded port `8000` in your Codespaces environment if you are using Codespaces, or visit `http://127.0.0.1:8000` on the same computer. Keep the port private; the API has no authentication. In the Paper Portfolio panel, choose Buy or Sell, enter a demo token label, quantity, and a manually chosen price, then submit. The default virtual balance is 1,000 units. Use a small example such as token `DEMO`, quantity `1`, price `10` to test a simulated buy.

**Scope of this demo:** it records manual virtual trades only. It does not yet automatically mirror monitored wallet transactions, fetch reliable token market prices, persist the paper portfolio across restarts, or share one ledger between dashboard and Telegram. Do not treat it as an automated copy-trading demo yet.

## Paper trading API

The dashboard exposes virtual-only endpoints:

- `GET /api/paper/status` — virtual cash, positions, pause state, and live-trading-disabled flag.
- `GET /api/paper/history` — recent simulated trade records.
- `POST /api/paper/control` with `{"action":"pause"}` or `{"action":"resume"}`.
- `POST /api/paper/trade` with `{"side":"buy","token":"DEMO","quantity":1,"price":10}` or the equivalent `sell` payload. These are manually priced simulations, not market orders.

The default virtual starting balance is 1,000 units. Position cost is capped at 25% of the starting balance per token by default. These limits are basic demo guardrails, not a complete trading risk engine.

## Telegram paper bot

The bot uses the Python standard library and controls only the virtual paper portfolio. Set these environment variables in your private terminal session (do not commit them):

- `TELEGRAM_BOT_TOKEN` — token from BotFather; keep it secret.
- `TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated numeric Telegram chat IDs allowed to use commands. If unset, the bot refuses to start.

From `Engen-main/Engen-main/`, start it with:

```bash
python -m engine.telegram_bot
```

Supported commands: `/start`, `/help`, `/status`, `/balance`, `/settings`, `/history`, `/pause`, and `/resume`. These commands do not place trades; they only report/control the simulated ledger. The bot is a separate process from the dashboard, so its in-memory ledger is separate and resets on restart.

## Configuration

Defaults are `ENGINE_MODE=read_only`, `LOG_LEVEL=INFO`, and `LIVE_TRADING_ENABLED=false`. Stage 1 rejects other engine modes and any value that attempts to enable live trading. Do not commit local `.env` files.

## Backups and duplicate files

Do not delete local `.bak` or `.pre_*.bak` files automatically. Compare each backup with its current file, preserve unique changes, and only then decide whether to archive or remove it. Remote GitHub history cannot show untracked files in a Codespaces workspace.

## Current audit

Audit changes are proposed in [Draft PR #6](https://github.com/santosreemangal-lgtm/Engen/pull/6). Review the changes and CI results before merging.
