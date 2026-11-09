# Engen — Honest Status

**Branch:** `feature/engen-vision-core-20261108`

## Is everything perfect?

**No.** Phase 1 paper engine is **usable and much stronger**, but the full vision (sub-100ms live detection, clean repo tree, production 24/7 proof) is **not** finished.

### Done (paper phase)

| Item | Status |
|------|--------|
| Automatic paper copy pipeline | Yes |
| SQLite persistence + restart tests | Yes |
| Latency stage timers (local) | Yes |
| Pending copy API + dashboard UI | Yes |
| Stale source gate (60s) | Yes |
| Safe entry: `run_dashboard.py` / `python -m dashboard` | Yes |
| No keys / no signing / no broadcast | Yes |

### Not perfect yet

| Item | Reality |
|------|---------|
| <100ms detection → fill | **Not proven** on live Solana; needs fast feed (gRPC), not only Python polling |
| Nested `Engen-main/Engen-main` | Still nested; cleanup is a separate big PR |
| Old stacked draft PRs | Not all merged/reconciled |
| 24/7 hosted proof | Not verified here |
| Market data | Public snapshots; can leave orders pending |

### Run

```bash
python run_dashboard.py
# or
python -m dashboard
```

### Safety

Paper only. Live trading requires a separate architecture and explicit owner approval.
