# Engen Multi-Agent System — Implementation Plan

**Status:** Foundation proposed on an isolated feature branch. No agent run has been executed
and no API key has been created or configured by this change.

## Goal

Add an optional OpenAI Agents SDK runtime inside Engen's own application. A single coordinator
routes bounded engineering tasks to connected specialist agents. The runtime must remain
separate from public-wallet monitoring and paper-simulation execution.

## Initial topology

    Phone / CLI (initial milestone: CLI only)
                 |
                 v
          Engen Master Coordinator
           /      |       |      \
          v       v       v       v
      Repository Coding   QA   Security
       Analyst  Planner  Agent  Agent
          \       |       |      /
           \      v       v     /
          Architecture / Research
                    |
                    v
            One consolidated plan

## Agent responsibilities

| Agent | Responsibility | Initial permissions |
|---|---|---|
| Master Coordinator | Task intake, delegation, dependency tracking, final report | Invoke specialist agents only |
| Repository Analyst | Inspect code, files, implementation status | Allowlisted source-file reads |
| Coding Planner | Propose minimal code changes and patches | Read-only; no file writes |
| QA Agent | Design and review tests; report evidence gaps | Read-only; no shell execution |
| Security Agent | Check secrets, access controls, and paper-only invariants | Read-only |
| Architecture Agent | Review design, dependencies, and migration order | Read-only |

## Delivery phases

### Phase 0 — Foundation
- Optional SDK dependency kept separate from the engine's standard-library offline tests.
- Coordinator and specialist definitions added in engen_agents/.
- Strict opt-in configuration and read-only source inspection tools.
- No API key creation, no credentials committed, and no calls to the OpenAI API during CI.

### Phase 1 — First live agent run
- Owner provisions an API key through the trusted OpenAI Platform flow.
- Owner stores the key as a private Codespaces environment secret.
- Install the optional dependency and run one narrowly scoped, read-only repository audit.
- Confirm model access, inspect SDK traces/usage, and record actual result output.

### Phase 2 — Controlled engineering tools
- Add read-only GitHub status/PR lookup.
- Add an approval-gated patch application tool scoped to a dedicated worktree/branch.
- Add bounded offline test execution in a dedicated environment.
- Add task/run audit records; redact secrets and cap retained output.
- Keep merge/deploy actions manual.

### Phase 3 — Mobile operator console
- Add a persistent task queue and authenticated task lifecycle endpoints.
- Add phone-friendly task status, pause, resume, cancel, and approval controls.
- Preserve localhost-only backend binding; use a separately reviewed HTTPS reverse proxy for remote access.
- Verify access control, CSRF/origin behavior, rate limiting, restart recovery, and mobile usability.

### Phase 4 — Reliability and expansion
- Add stale-run recovery, bounded retries, concurrency limits, per-task budgets, and failure reporting.
- Add CI integration and regression coverage.
- Add more specialists only when a specific workload justifies them.

## Mandatory invariants

- ENGINE_MODE=read_only and LIVE_TRADING_ENABLED=false.
- No seed phrases, private keys, signing, transaction submission, or broadcasting.
- No investment advice, token recommendations, wallet-copy signals, or buy/sell timing analysis.
- No agent can autonomously modify files, execute shell commands, push commits, merge, deploy, or alter runtime settings in Phase 0/1.
- Every future high-impact tool requires human approval, narrow scope, tests, and audit logging.
- Agents are request-driven. No background activity is claimed unless an external scheduler is implemented and explicitly enabled.

## Acceptance criteria for Phase 0

- Existing offline unit tests and compile check remain unaffected because the SDK is optional.
- Agent configuration fails closed if not explicitly enabled, if the API key is absent, if engine mode is not read-only, or if live trading is enabled.
- File inspection blocks traversal, runtime data directories, credentials, databases, logs, unsupported file types, and oversized files.
- An agent task is bounded by a maximum of 12 SDK turns.
- The first run is only declared verified after an actual run result and trace/usage inspection.
