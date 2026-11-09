# Engen — Vision Roadmap

**Branch:** `feature/engen-vision-core-20261108`  
**Language:** Python for paper phase (Rust optional later for detection hot-path only)  
**Goal:** Reliable ultra-low-latency **Paper** copy engine first. Real trading stays disabled.

---

## Progress

### Phase 1 — Paper copy reliable (this branch)

- [x] ROADMAP / STATUS documented
- [x] `paper_copy_runner` latency hooks + pending status helper
- [x] `monitor_bridge` latency wiring
- [x] Restart-recovery tests (fill + pending survive new process instance)
- [x] `pending_copy_report` + unit tests
- [x] `server_with_pending.py` entrypoint (API includes `pending_copy`)
- [x] `pending_copy_ui.js` for dashboard metric
- [ ] Optional: one-line script include in `index.html` (see STATUS.md)
- [ ] Flatten nested directories (optional cleanup)

### Phase 2 — Structure + polish
- Nested folder cleanup
- Single clear README entrypoint

### Phase 3 — Latency path
- Instrument detection P50/P95/P99 end-to-end
- Evaluate Yellowstone gRPC (likely Rust sidecar later)

---

## Safety (never violate)

- No private keys / seeds
- No signing / broadcast
- Paper only in this phase
- Live mode = separate approval + architecture

See `STATUS.md` for run commands.
