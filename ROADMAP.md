# Engen — Roadmap

**Branch:** `feature/engen-vision-core-20261108`

## Phase 1 — Paper engine (this branch) ✅

- [x] Paper copy pipeline + sizing
- [x] SQLite persistence + restart tests
- [x] Latency hooks
- [x] Pending status API + UI
- [x] Stale source gate
- [x] Canonical entrypoints
- [x] `STATUS.md`, `README_PAPER.md`
- [x] `LIVE_BOUNDARY.md`, `LIVE_READINESS.md`

## Phase 2 — Repo cleanup (optional, later PR)

- [ ] Flatten `Engen-main/Engen-main`
- [ ] Reconcile older draft PRs

## Phase 3 — Faster detection (later)

- [ ] Real-feed P50/P95/P99
- [ ] gRPC / shred path if polling is too slow

## Phase 4 — Live execution (only after checklist)

- [ ] Separate live package + process
- [ ] Complete every item in `LIVE_READINESS.md`
- [ ] Respect `LIVE_BOUNDARY.md`
- [ ] Explicit owner approval + tiny pilot only

**Default remains paper-only.**
