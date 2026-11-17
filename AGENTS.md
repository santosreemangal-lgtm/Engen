# Engen agent workspace rules

These instructions apply to every coding agent, terminal tool, and automated reviewer
working in this repository.

## Scope and allowed engineering work

- Work only on software engineering, tests, documentation, developer tooling, and the
  read-only/paper-simulation implementation.
- You may inspect and edit repository source, tests, and documentation.
- You may run normal development commands such as formatting, compilation, unit tests,
  type checks, package metadata inspection, and local build commands.
- You may use public technical documentation, package registries, and public reference
  repositories. Prefer official primary sources and record the exact URLs/versions.
- Keep changes small, preserve unrelated work, and inspect git status and git diff
  before and after making changes.

## Never access these items

Do not open, print, search, upload, copy, or transmit:
- .env, .env.* files, key files, certificate files, tokens, passwords, or credential stores
- data/, profiles/, runtime databases, SQLite files, raw signature stores, logs,
  local wallet lists, or user-specific wallet addresses
- backups, .bak files, .pre_*.bak files, or local machine configuration
- .git/config, Git credential helpers, shell history, or environment variable dumps

Do not run env, printenv, set, export -p, or commands intended to reveal credentials.
The Codex subprocess receives a minimal environment; shell child processes must not receive
OpenAI or Codex API keys, GitHub tokens, Telegram tokens, or other credentials.

## Git and environment operations

- Never run git push, force-push, merge, release, publish, or deploy.
- Never run git reset --hard, git clean, rm -rf, broad recursive deletion, or any
  command that deletes user data, databases, logs, or backups.
- Do not change repository remotes, branch protections, CI secrets, Codespaces secrets,
  environment variables, or account settings.
- Do not install global packages or modify the base Codespaces image. Project-local
  development dependencies may be installed when required by the task.

## Engen safety invariant

- Keep public-chain observation read-only and all portfolio activity virtual/paper-only.
- Never add transaction signing, private-key handling, transaction submission, or
  transaction broadcasting.
- Do not make token recommendations, trading-strategy suggestions, or buy/sell signals.
- A passing test suite is not proof of financial performance or production readiness.

## Verification and reporting

- Run the narrowest relevant test first, then the broader offline suite when appropriate.
- Report exact commands and observed exit outcomes. Do not claim a test passed without
  actual output.
- Inspect and summarize every changed file. State remaining limitations and blockers.
