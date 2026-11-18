# Autonomous Engen Progress

## 2026-11-17 — Rust dashboard profile deletion parity

**Status: VERIFIED — this narrow API parity fix passed GitHub Actions. Overall Rust-only migration remains NOT READY.**

### Repository state verified before the change

- Repository: `santosreemangal-lgtm/Engen`
- Default branch: `main` at `ba501c43260cd7569714b58f84eb0bf11967f444`
- Canonical migration review PR #48: Draft, open, unmerged.
- Rust dashboard runtime PR #49: Draft, open, unmerged.
- Durable monitor lease PR #50: Draft, open, unmerged.
- PR #50 head/base evidence: `4a5ca8ae292934f2af0c1b45f69c2a40d2a61d03` based on `migration/rust-dashboard-runtime-20261113`.
- PR #50 Rust Engine Checks run #651 completed successfully before this change: https://github.com/santosreemangal-lgtm/Engen/actions/runs/43891403712

### Acceptance item

Restore the Rust `POST /api/profiles` `action=delete` behavior to match the existing Python API while preserving soft-delete and audit-history semantics.

### Evidence and change

The Python `dashboard/server.py` handler supports `action == "delete"`. In the Rust `dashboard_server.rs`, the deletion implementation was present inside `post_profiles`, but the enclosing match arm omitted `"delete"`; therefore that implementation was unreachable and profile deletion returned the unsupported-action response.

The match arm now includes `"delete"`, making the existing stop-monitor, clear-monitor-opt-in, and soft-delete path reachable.

A regression test uses the dashboard fixture's temporary SQLite databases. It creates a profile and transaction, calls the Rust API delete route, then verifies:
- HTTP 200 and `deleted=true` / `history_preserved=true`;
- the profile is soft-deleted and no longer returned as active;
- the transaction-history row remains available;
- the deleted profile is removed from persisted monitor opt-ins.

### Branch and commit

- Base branch: `fix/rust-profile-monitor-durable-lease-20261116`
- Base SHA: `4a5ca8ae292934f2af0c1b45f69c2a40d2a61d03`
- Feature branch: `fix/rust-profile-delete-api-parity-20261117`
- Implementation commit: `81594229a62b17b59ef5e0e0edee6eded351cd2c`
- Formatting-fix commit: `3058cf5e670e7096c0658035a116f0014ee5185d`
- Feature PR: [#52 — Rust profile deletion API parity](https://github.com/santosreemangal-lgtm/Engen/pull/52), Draft and unmerged.
- Changed implementation file: `Engen-main/Engen-main/rust-engine/src/dashboard_server.rs`

### Verification

- Local Rust commands: **NOT RUN**. This execution environment does not have `rustc` or `cargo` installed.
- First PR run #652 failed only at `cargo fmt --all -- --check`; the formatter's requested multiline match-arm layout was applied in commit `3058cf5e670e7096c0658035a116f0014ee5185d`.
- Final PR run #653: **SUCCESS** — https://github.com/santosreemangal-lgtm/Engen/actions/runs/44012257288
- All 14 workflow steps passed: formatting; `cargo test --all-targets`; public read-only RPC smoke test; selected Python Phase 2 and Phase 8 reference tests; dashboard/browser syntax; Rust launcher smoke test; localhost dashboard process E2E; release binary check with Python environment variables unset; Clippy with warnings denied; and the report-only migration audit.
- Rust test evidence: 221 library tests + 3 binary unit tests + 4 CLI smoke tests + 1 public RPC test + 7 transaction-pipeline tests passed (236 total across `cargo test --all-targets`). The explicit public RPC workflow step also passed.
- The static migration audit script completed successfully but still reports **38 Python production/runtime blockers** and states `RUNTIME GATE: NOT READY`. A green report-only audit is not production migration acceptance.
- No real `data/` database, secret, wallet key, production process, or deployment was accessed or changed.

### Remaining gaps and next task

This fix closes one concrete dashboard API parity defect only. It does not certify the full dashboard API contract, profile authorization, whole-process restart recovery, or Rust-only production cutover. Keep PRs #48–#52 Draft/unmerged.

Next priority: complete a route-by-route Python/Rust Dashboard API contract matrix, then add regression coverage for any remaining response/status/security mismatches before moving to whole-application restart and database-compatibility E2E.


## 2026-11-18 — Fail closed on profile monitor-state persistence errors

**Status: PARTIAL — implementation and regression test committed; current-commit CI is NOT TESTED yet.** The Rust-only runtime remains NOT READY.

### Repository and stack state

- Repository: `santosreemangal-lgtm/Engen`
- Default branch: `main` at `ba501c43260cd7569714b58f84eb0bf11967f444`.
- Canonical migration PR #48: Draft, open, unmerged; branch `rewrite/rust-backend-20261109`, head `b0df77d2775e4b4280442cf35ea64f40c4292d6b`.
- Parent stack PR #49 (Rust dashboard runtime), #50 (durable monitor leases), and #52 (profile-delete route parity): all Draft/open/unmerged at inspection.
- This change is stacked on PR #52 branch `fix/rust-profile-delete-api-parity-20261117`, base SHA `f043fc619a6134438550295673e0f6734085a193`.
- Feature branch: `fix/rust-delete-monitor-pref-fail-closed-20261118`.
- Rust implementation commit: `4c5107d9877d8c745037622eb5f3d686dc2dcc82`.
- Draft PR: [#54 — Fail closed when Rust profile monitor state cannot be saved](https://github.com/santosreemangal-lgtm/Engen/pull/54).

### Acceptance item and source evidence

The Python `POST /api/profiles` `action=delete` handler stops the profile monitor, calls `set_profile_monitor_persistence(profile_id, False)`, and only then soft-deletes the profile. An `OSError` is returned as HTTP 503, so a failed persistence write does not proceed to deletion.

The Rust handler previously discarded the result from `set_profile_monitor_pref(&id, false)` and proceeded to `ProfileStore::delete_profile` even if the monitor opt-out state could not be saved. This was a concrete Python/Rust API and restart-state parity defect.

### Change and tests

Changed only `Engen-main/Engen-main/rust-engine/src/dashboard_server.rs`:

- Propagate the existing HTTP 503 response when monitor-state persistence fails.
- Add `dashboard_api_delete_profile_keeps_profile_when_monitor_preference_persistence_fails`.
- The test uses the dashboard fixture's disposable temporary SQLite database and creates a directory at the monitor-state file path to force the atomic rename to fail deterministically.
- The test asserts HTTP 503 and confirms the profile remains active.
- No new dependency or schema change.

### Verification

- Local Rust commands: **NOT RUN**; this GitHub-only execution session has no local Rust toolchain.
- Parent PR #52 head `f043fc619a6134438550295673e0f6734085a193` had Rust Engine Checks run #654 completed successfully: [run #654](https://github.com/santosreemangal-lgtm/Engen/actions/runs/44012849889). That run does **not** include this new change.
- For implementation commit `4c5107d9877d8c745037622eb5f3d686dc2dcc82`, no GitHub Actions run or commit status was visible at the time this record was written. Current-commit CI is therefore **NOT TESTED**, not passed.
- The PR remains Draft and unmerged. No real `data/` database, secret, wallet key, production process, deployment, or live execution capability was touched.

### Remaining gaps and next task

- First, obtain a GitHub Actions run for PR #54 and verify the new failure-path regression test plus all existing Rust checks.
- Then complete the route-by-route Python/Rust Dashboard API contract and security matrix; add focused regressions only for evidenced mismatches.
- Whole-process crash/restart and disposable-database compatibility E2E, remaining Rust runtime ports, and the local paper-fill latency benchmark remain open.
- The last static migration audit on the parent stack still reports 38 Python production/runtime blockers and `RUNTIME GATE: NOT READY`; this change does not reduce that blocker count or certify production readiness.
