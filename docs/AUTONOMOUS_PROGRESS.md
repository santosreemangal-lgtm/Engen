# Autonomous Engen Progress

## 2026-11-17 — Rust dashboard profile deletion parity

**Status: PARTIAL — implementation committed; GitHub Actions verification pending.**

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
- Implementation commit before this progress note: `81594229a62b17b59ef5e0e0edee6eded351cd2c`
- Changed implementation file: `Engen-main/Engen-main/rust-engine/src/dashboard_server.rs`

### Verification

- Local Rust commands: **NOT RUN**. This execution environment does not have `rustc` or `cargo` installed.
- GitHub Actions: **PENDING** for the feature PR.
- Expected CI checks: Rust formatting, all Rust unit/integration targets, public read-only RPC smoke test, selected Python behavioral references, dashboard/browser checks, launcher E2E, Python-free release check, Clippy, and the report-only migration audit.
- No real `data/` database, secret, wallet key, production process, or deployment was accessed or changed.

### Remaining gaps and next task

This fix closes one concrete dashboard API parity defect only. It does not certify the full dashboard API contract, profile authorization, whole-process restart recovery, or Rust-only production cutover. Keep PRs #48–#50 and this PR Draft/unmerged.

Next priority: complete a route-by-route Python/Rust Dashboard API contract matrix, then add regression coverage for any remaining response/status/security mismatches before moving to whole-application restart and database-compatibility E2E.
