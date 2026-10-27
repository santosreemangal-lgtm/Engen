# Engen — Solana Wallet Monitor

Engen is an early-stage, **read-only Solana wallet monitor**. The current project is intended for offline tests and paper-trading development only.

## Safety status

- Live trading is not implemented in the Stage 1 configuration and must remain disabled.
- The engine reads public blockchain data; it must not ask for seed phrases, private keys, or wallet passwords.
- Monitoring is opt-in. Starting the dashboard server does not automatically start a scan.
- Do not expose the dashboard backend to an untrusted network. The current dashboard API has no user authentication.
- Treat all paper-trading results as simulations, not financial advice or evidence of real-world profitability.

## Repository layout

The canonical application currently lives at `Engen-main/Engen-main/`. The repository-root `main.py` is a compatibility entry point that delegates to the canonical application. Some legacy modules and tests remain under `Engen-main/Engen-main/engine/engine/engine/`; these should only be moved or removed after import references and test coverage are verified.

## Requirements

- Python 3.10 is used by CI.
- The current offline test suite uses the Python standard library; no package installation is required for the mock test suite.
- Public Solana RPC calls require network access when those features are explicitly used.

## Run offline checks

From the repository root:

```bash
python -m compileall -q Engen-main/Engen-main
python -m unittest discover -s Engen-main/Engen-main/engine/engine/engine/tests -v
python main.py --mock-analysis-test
```

The compatibility entry point can also be checked with:

```bash
python ../../main.py --mock-analysis-test
```

Run that last command from `Engen-main/Engen-main/`.

## Configuration

The engine defaults to `ENGINE_MODE=read_only`, `LOG_LEVEL=INFO`, and `LIVE_TRADING_ENABLED=false`. Stage 1 rejects other engine modes and any value that attempts to enable live trading. Do not put credentials in source code or commit local `.env` files.

## Dashboard

The dashboard backend is a development component and does not currently provide account authentication. Only run it in a trusted environment and do not publish its port to the public internet. Review access controls before any deployment.

## Backups and duplicate files

Do not delete local `.bak` or `.pre_*.bak` files automatically. Compare each backup with the corresponding current file first, confirm that any unique changes are preserved, and only then decide whether to archive or remove it. Remote GitHub history does not show untracked files in a Codespaces workspace.

## Current audit

The repository audit fixes are proposed in [Draft PR #6](https://github.com/santosreemangal-lgtm/Engen/pull/6). Review and merge the PR only after reviewing the changes and CI results.