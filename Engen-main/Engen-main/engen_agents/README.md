# Engen multi-agent development runtime

This is an optional software-engineering assistant for the Engen repository. It is not part
of the chain-monitoring worker and it does not start when the dashboard starts.

## Initial agent network

- **Master Coordinator** — owns the task plan, delegates subtasks, and reconciles results.
- **Repository Analyst** — inspects source files and maps current implementation.
- **Coding Planner** — proposes small patches; it cannot write files.
- **QA Agent** — reviews existing tests and proposes regression tests.
- **Security Agent** — checks secrets, access controls, and the paper-only boundary.
- **Architecture Agent** — reviews integration decisions and migration risks.

The coordinator uses the Agents SDK Agent.as_tool() pattern so specialists return bounded
subtask results to one manager. Coordinator runs are capped at 12 turns and each
specialist tool-run is capped at 4 turns. This first milestone is deliberately read-only.

## Install

Run from the canonical app directory, Engen-main/Engen-main:

    python -m pip install -r requirements-agents.txt

## Configure privately

Set these in Codespaces secret/environment settings, not in source files:

- OPENAI_API_KEY — an OpenAI Platform API key.
- ENGEN_AGENTS_ENABLED=true — explicit opt-in to run the agent service.
- ENGINE_MODE=read_only
- LIVE_TRADING_ENABLED=false
- Optional: ENGEN_AGENT_MODEL=gpt-5.6
- Optional: ENGEN_AGENT_MAX_TURNS=8 (allowed range: 1–12).

The runtime never prints, returns, or stores the API key. The SDK reads it from the process
environment. Do not paste keys, tokens, private addresses, or credentials into prompts.

## Run one task

    python -m engen_agents "Inspect the dashboard API boundaries and propose a test plan."

Each invocation performs one run and exits. It does not run in the background, keep a daemon
alive, or resume itself. API usage is billed separately from a ChatGPT subscription.

## Tools and permissions in this milestone

Agents can read only UTF-8 source/documentation files with allowlisted extensions inside the
Engen repository root. Runtime data, databases, logs, environment files, key files,
wallet-list JSON, and files outside the application directory are blocked. Secret-like
environment assignments and OpenAI key-shaped strings are redacted before a file is returned.

No shell, network-control, database, file-write, GitHub-write, merge, deployment, wallet,
signing, or transaction-broadcast tools are exposed. Any future write-capable tool must have
a human approval gate, an explicit allowlist, audit logging, and tests before it is enabled.

Repository source files are sent to the OpenAI API for the requested analysis. Review the
files you ask an agent to inspect and do not expose sensitive source or personal data.

## Mobile control roadmap

The CLI is only the first milestone. Mobile controls are a later phase:
1. Persisted task queue with explicit status and cancellation.
2. Authenticated dashboard endpoints for submit/pause/resume/stop.
3. A mobile-friendly task board with live status and bounded logs.
4. Approval prompts for patch application and GitHub writes.
5. Restart recovery and audit records.

Do not expose the current localhost dashboard directly to the public internet. Remote access
requires a separately reviewed HTTPS/authentication design.
