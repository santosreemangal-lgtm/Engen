# Engen — Status (feature/engen-vision-core-20261108)

## Language

**Python stays** for the paper engine and dashboard.

- Local paper fill + SQLite is not the bottleneck
- Sub-100ms end-to-end is mostly **RPC / feed latency**
- Later optional: Rust sidecar only for Yellowstone/gRPC detection

---

## What replaced / unified previous work

| Piece | Role |
|-------|------|
| `engine/paper_copy_runner.py` | Queue + execute paper copy, latency hooks, pending helper |
| `engine/pending_copy_report.py` | Attach `pending_copy` to API reports |
| `dashboard/server_with_pending.py` | **Canonical dashboard** — API pending + injects UI script into HTML |
| `dashboard/pending_copy_ui.js` | Pending Copy Orders metric in the browser |
| `run_dashboard.py` (root + nested) | One-command start |
| Restart / pending unit tests | Prove ledger survives process restart |

Original `dashboard/server.py` is **unchanged** (safe). Use `server_with_pending.py` instead — it wraps the same handlers.

---

## Run

```bash
# from repo root
python run_dashboard.py

# or from Engen-main/Engen-main
python run_dashboard.py
# or
python dashboard/server_with_pending.py
```

Open the printed localhost URL. Portfolio should show pending copy after paper-simulation loads.

---

## Safety

Paper only. No private keys, signing, or broadcast.

---

## Optional later

1. Flatten `Engen-main/Engen-main` tree
2. Merge old draft PRs carefully onto this line of work
3. Faster detection feed (gRPC) when you outgrow polling
