# Engen — Solana Wallet Monitor

Engen is an early-stage, **read-only Solana wallet monitor** intended for offline tests and paper-trading development only.

## Safety status

- Live trading must remain disabled; Stage 1 rejects attempts to enable it.
- Never put seed phrases, private keys, or wallet passwords in source code or chat.
- Monitoring is opt-in; starting the dashboard server does not automatically start a scan.
- The dashboard API has no user authentication. Run it only in a trusted development environment and do not expose it publicly.
- Paper-trading results are simulations, not evidence of real-world profitability.

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

## Configuration

Defaults are `ENGINE_MODE=read_only`, `LOG_LEVEL=INFO`, and `LIVE_TRADING_ENABLED=false`. Stage 1 rejects other engine modes and any value that attempts to enable live trading. Do not commit local `.env` files.

## Backups and duplicate files

Do not delete local `.bak` or `.pre_*.bak` files automatically. Compare each backup with its current file, preserve unique changes, and only then decide whether to archive or remove it. Remote GitHub history cannot show untracked files in a Codespaces workspace.

## Current audit

Audit changes are proposed in [Draft PR #6](https://github.com/santosreemangal-lgtm/Engen/pull/6). Review the changes and CI results before merging.
