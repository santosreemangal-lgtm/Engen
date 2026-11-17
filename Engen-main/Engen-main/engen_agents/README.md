# Engen multi-agent development runtime

This is an optional software-engineering team inside Engen. It runs only when explicitly
started; it does not start with the dashboard or the public-wallet monitor.

## Agent network

- **Master Coordinator** — breaks requests into subtasks, delegates, reconciles findings,
  tracks acceptance criteria, and reports blockers.
- **Repository Analyst** — maps implementation from source files and tests.
- **Workspace Coding Agent** — uses the Codex workspace tool to edit repository code,
  run terminal commands, install project-local development dependencies, and verify tests.
- **QA Agent** — reviews test coverage and proposes regression cases.
- **Security Agent** — reviews secrets, permissions, APIs, and the paper-only invariant.
- **Architecture and Research Agent** — researches official technical documentation and
  public reference repositories, then compares patterns with Engen.

The Coordinator uses Agents SDK Agent.as_tool() to connect the specialists. Each specialist
run is bounded. The Workspace Coding Agent delegates actual workspace operations to the
Agents SDK's experimental Codex tool.

## Codespaces quick start

The agent foundation is on the draft branch
`feature/engen-agent-foundation-20261117`, not on `main`.

If the Codespace was created from `main`, switch to the agent branch in its terminal:

    git fetch origin feature/engen-agent-foundation-20261117
    git switch --track origin/feature/engen-agent-foundation-20261117

Then run a no-API preflight from this directory:

    bash run_agents.sh --preflight

The preflight confirms that the Codespaces secret is available without printing it,
constructs the coordinator and five specialists, and makes no OpenAI API request.

To run one bounded engineering task:

    bash run_agents.sh "Inspect the Rust and Python pipeline and report the next verified engineering blockers. Do not change files."

The launcher installs the optional dependency only when it is missing. It sets
`ENGEN_AGENTS_ENABLED=true`, `ENGINE_MODE=read_only`, and
`LIVE_TRADING_ENABLED=false` for the process.

## Install

Manual installation from this directory is also supported:

    python -m pip install -r requirements-agents.txt

The optional SDK dependency is separate from the existing engine requirements so the
offline engine tests do not need the Agents SDK.

## Configure privately

Set these in Codespaces secret/environment settings, not in source files:

- OPENAI_API_KEY — an OpenAI Platform API key for the coordinator and model requests.
- ENGEN_AGENTS_ENABLED=true — explicit opt-in to run the agent service (the launcher sets it).
- ENGEN_CODEX_ENABLED=true — enable the Codespaces workspace tool (defaults to true).
- ENGINE_MODE=read_only (the launcher sets it).
- LIVE_TRADING_ENABLED=false (the launcher sets it).
- Optional: ENGEN_CODEX_WORKDIR — an existing directory inside the current Engen checkout;
  defaults to the repository root.
- Optional: ENGEN_AGENT_MODEL=gpt-5.6-sol
- Optional: ENGEN_AGENT_MAX_TURNS=8 (allowed range: 1–12).
- Optional: CODEX_API_KEY — a separate key for Codex; if absent, OPENAI_API_KEY is reused.

Never paste an API key into chat, source files, command arguments, or committed configuration.
The app process uses OPENAI_API_KEY. The Codex CLI receives CODEX_API_KEY in its own process
environment, but its shell children use an explicit non-secret environment allowlist. The
allowlist does not include OPENAI_API_KEY, CODEX_API_KEY, GitHub tokens, or Telegram tokens.
Codex also runs with a private temporary HOME so it does not use the user's normal GitHub
credential store.

## Workspace and terminal permissions

The workspace coding agent runs through the Agents SDK Codex tool:

- **Workspace:** Codex working_directory defaults to the repository root. It can be pointed
  at another existing directory inside this checkout.
- **File access:** workspace-write permits source and test edits inside that workspace.
- **Terminal:** it can inspect Git status/diffs, run Python and Rust checks, format code,
  run tests, and install project-local development dependencies when required.
- **External research:** public network access is enabled for technical documentation,
  package registries, and public source repositories. The other specialists also have the
  hosted WebSearchTool for sourced documentation research.
- **Approval:** approval_policy is on-request. Ordinary operations inside the workspace can
  proceed without repeatedly asking; actions that need permission escalation pause for review.
- **GitHub changes:** push, force-push, merge, release, publish, deployment, and remote/runtime
  settings remain manual. The current app does not connect a private GitHub write credential
  to Codex.

This tool runs where the Engen agent process runs. To work against your Codespaces checkout,
run the agent service inside that Codespaces container and keep ENGEN_CODEX_WORKDIR inside
that checkout.

## Files, data, and safety

Repository instructions are in the root AGENTS.md. Agents are instructed not to open, print,
search, copy, or transmit .env files, credentials, local wallet lists, profiles, databases,
logs, backups, or local Git credentials. Shell child processes do not receive API keys or
other credential environment variables. Do not put secrets or private runtime data in the
coding workspace.

The sandbox limits writes to the configured workspace; it is not a substitute for keeping
secrets and private runtime data outside that workspace. The Coding Agent must follow
AGENTS.md, inspect Git status before editing, preserve unrelated changes, and inspect its
diff after editing. Never grant the agent live wallet, signing, or broadcast capabilities.

No shell, wallet, database, live-execution, push, merge, or deployment permissions are given
to the other specialist agents. They remain source/research/review agents.

Each invocation performs one bounded run and exits. It does not run in the background or
resume itself. API usage is billed separately from a ChatGPT subscription.

## Mobile control roadmap

The CLI is only the first milestone. Mobile controls remain a later phase:
1. Persisted task queue with explicit status, cancellation, and restart recovery.
2. Authenticated dashboard endpoints for submit/pause/resume/stop.
3. A phone-friendly task board with progress, approvals, and bounded logs.
4. Approval prompts for patch application and GitHub writes.
5. Audit records, concurrency limits, retry policies, and per-task budgets.

Do not expose the current localhost dashboard directly to the public internet. Remote access
requires a separately reviewed HTTPS/authentication design.
