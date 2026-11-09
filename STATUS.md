# Engen — Branch Status

**Branch:** `feature/engen-vision-core-20261108`  
**Language:** Python (correct for paper phase; Rust optional later for hot-path detection)  
**Mode:** Paper trading only — no keys, no signing, no broadcast

---

## Done on this branch

| Item | Path / notes |
|------|----------------|
| Vision roadmap | `ROADMAP.md` |
| Paper-copy latency hooks | `engine/paper_copy_runner.py` |
| Monitor latency wiring | `dashboard/monitor_bridge.py` |
| Restart recovery tests | `engine/engine/engine/tests/test_paper_copy_restart.py` |
| Pending status helper | `engine/pending_copy_report.py` |
| Pending helper tests | `engine/engine/engine/tests/test_pending_copy_report.py` |
| Dashboard entry + pending API | `dashboard/server_with_pending.py` |
| Pending UI script | `dashboard/pending_copy_ui.js` |
| Manual patch notes | `docs/PENDING_COPY_DASHBOARD_PATCH.md` |

---

## How to run (paper dashboard with pending copy)

From `Engen-main/Engen-main`:

```bash
python dashboard/server_with_pending.py
```

Optional: in `dashboard/index.html` before `</body>`, add:

```html
<script src="/pending_copy_ui.js"></script>
```

(If static files are served from the dashboard folder, this loads the metric automatically.)

---

## Language decision

- **Python stays** for paper engine, dashboard, SQLite ledger, tests.
- Bottleneck for <100ms is **network/RPC**, not Python local fill logic.
- Later: optional **Rust** service for Yellowstone gRPC / shred detection only; keep Python for paper + UI.

---

## Still later (not blocking paper engine)

1. Flatten nested `Engen-main/Engen-main` folders
2. One-line script tag in `index.html` (or apply `docs/PENDING_COPY_DASHBOARD_PATCH.md`)
3. Faster live detection (gRPC) when you move beyond polling
4. Merge stacked historical draft PRs carefully

---

## Safety

Never enable live trading on this branch without a separate, explicit architecture and owner approval.
