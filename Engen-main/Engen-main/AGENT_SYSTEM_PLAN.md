# Engen Multi-Agent System — Implementation Plan

**Status:** Agent foundation and workspace-access implementation are proposed on an isolated
feature branch. No real agent run has been executed and no API key has been created or
configured by these code changes.

## Goal

Run an interconnected OpenAI Agents SDK team inside Engen's own backend. The Master
Coordinator assigns work to specialist agents. A single Coding Agent receives workspace
and terminal capabilities through the Codex tool; research and review agents remain
read-only. This system is separate from public-wallet monitoring and paper simulation.

## Initial topology

    Phone / CLI
         |
         v
    Engen Master Coordinator
       /   |    |    |    \
      v    v    v    v     v
    Repo  Code  QA  Security Architecture
    Analyst Agent Agent Agent   + Research
             |
             v
       Codex Workspace Tool
       - Source/test edits
       - Terminal/build/test
       - Public network research
             |
             v
       One consolidated report

## Agent responsibilities and permissions

| Agent | Responsibility | Permissions |
|---|---|---|
| Master Coordinator | Task intake, delegation, dependency tracking, final report | Calls specialist agents |
| Repository Analyst | Inspect source and describe actual implementation | Allowlisted source-file reads |
| Workspace Coding Agent | Implement code changes and run verification | Codex workspace-write, terminal, public network |
| QA Agent | Review tests and design regression coverage | Source reads and public technical research |
| Security Agent | Review access controls, secrets, and paper-only constraints | Source reads and public security research |
| Architecture/Research Agent | Compare official docs and public source repositories with Engen | Source reads and public web search |

## Delivery phases

### Phase 0 — Foundation
- Optional Agents SDK dependency isolated from engine requirements.
- Master Coordinator and specialist agent definitions added.
- Read-only source inspection tool with path, symlink, size, and secret-pattern checks.
- Offline configuration and source-access regression tests.
- No API key creation or API calls during CI.

### Phase 1 — Workspace and research capability
- Workspace Coding Agent integrated with the Agents SDK Codex tool.
- Codex uses workspace-write mode, bounded to the configured Engen checkout.
- Shell subprocesses receive only an explicit non-secret environment allowlist.
- API credentials are provided only to the Codex process, not child shell commands.
- Approval policy is on-request; routine workspace edits and checks should not need repeated
  confirmation, while permission escalation is reviewed.
- Public web research is available through WebSearchTool and Codex network access.
- Git push, merge, deployment, destructive deletion, and runtime-setting changes remain manual.

### Phase 2 — First real agent run and verification
- Owner provisions or reuses an OpenAI Platform API key through the trusted setup flow.
- Owner stores it as a private Codespaces environment secret.
- Install the optional dependency and run one narrow software audit in Codespaces.
- Confirm Codex CLI availability, model access, tool invocation, and usage/traces.
- Run offline tests, Rust tests as available, compile checks, and review the exact diff.
- Do not mark this phase verified until actual outputs are recorded.

### Phase 3 — Controlled GitHub integration
- Add read-only PR/check/issue lookup for the agent runtime.
- Add approval-gated patch sync or branch write operations with narrow permissions.
- Keep push/merge/deploy as separate owner-approved operations.
- Add audit logs and tests proving that secrets and private runtime data are not sent to tools.

### Phase 4 — Mobile operator console
- Add a persistent task queue with explicit lifecycle states.
- Add authenticated submit/pause/resume/cancel endpoints.
- Add a mobile-friendly task board with progress, approvals, and bounded logs.
- Add process-restart recovery, stale-run handling, and audit records.
- Preserve localhost-only backend binding; remote access requires separately reviewed HTTPS
  and authentication controls.

### Phase 5 — Reliability and expansion
- Add concurrency limits, retries, per-task budgets, rate limits, and usage reporting.
- Add more specialists only when a concrete workload justifies them.
- Add CI integration and regression tests for orchestration and recovery.

## Mandatory invariants

- ENGINE_MODE=read_only and LIVE_TRADING_ENABLED=false.
- No seed phrases, private keys, wallet custody, signing, transaction submission, or broadcast.
- No token recommendations, investment advice, wallet-copy signals, or buy/sell timing advice.
- Agents work on software engineering, tests, and technical documentation only.
- The coding tool may edit only within its configured workspace.
- Agents must not read .env files, local runtime databases, wallet lists, logs, backups,
  local credential stores, or Git credentials.
- Shell child processes must not receive OpenAI/Codex API keys, GitHub tokens, or bot tokens.
- Push, force-push, merge, release, publish, deploy, and runtime setting changes remain manual.
- Do not delete user data or backups. Preserve unrelated modifications.
- All claims of successful verification require exact command output or CI evidence.
- Agents are request-driven. No background activity is claimed without an external scheduler.

## Phase 1 acceptance criteria

- Existing offline engine tests remain independent of the optional SDK.
- Agent runtime fails closed when not opted in, when API key is missing, or if live trading is
  enabled / engine mode is not read-only.
- Workspace path must resolve inside the Engen repository.
- Codex CLI gets workspace-write access only for the configured workspace.
- The child shell environment excludes API keys, GitHub tokens, and Telegram tokens.
- Codex network access is on-request/approved for permission escalation and used for public
  technical sources only.
- Agent turns and specialist calls are bounded.
- No agent can push, merge, deploy, or enable live execution.
