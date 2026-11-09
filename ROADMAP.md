# Engen — Vision Roadmap

**Branch:** `feature/engen-vision-core-20261108`  
**Language:** Python (paper phase)  
**Status:** Phase 1 complete on this branch

---

## Phase 1 — Paper copy reliable ✅

- [x] ROADMAP / STATUS
- [x] paper_copy_runner latency + pending helpers
- [x] monitor_bridge latency wiring
- [x] Restart-recovery tests
- [x] pending_copy_report + tests
- [x] server_with_pending (API + HTML UI inject)
- [x] pending_copy_ui.js
- [x] run_dashboard.py entrypoints
- [x] README_PAPER.md

## Phase 2 — Optional cleanup

- [ ] Flatten nested `Engen-main/Engen-main` (large move; do in dedicated PR)
- [ ] Merge/reconcile older draft PRs onto this line

## Phase 3 — Faster detection (later)

- [ ] End-to-end P50/P95/P99 measurement under real RPC
- [ ] Yellowstone gRPC / shred path (likely Rust sidecar)

---

## Safety

No keys, no signing, no broadcast. Live trading requires separate approval.

See `STATUS.md` and `Engen-main/Engen-main/README_PAPER.md`.
