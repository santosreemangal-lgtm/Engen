# Engen — Status (final for this branch scope)

**Branch:** `feature/engen-vision-core-20261108`

## Scope closed on this branch

### Paper engine
| Item | Status |
|------|--------|
| Automatic paper copy | Done |
| SQLite + restart tests | Done |
| Latency stage timers | Done |
| Pending API + dashboard UI | Done |
| Stale source gate (60s) | Done |
| `run_dashboard.py` / `python -m dashboard` | Done |
| `README_PAPER.md` | Done |
| No keys / signing / broadcast | Done |

### Docs for future live (not live code)
| Item | Status |
|------|--------|
| `LIVE_BOUNDARY.md` | Done |
| `LIVE_READINESS.md` | Done |

## Explicitly out of scope here

| Item | Status |
|------|--------|
| Real signing / broadcast | **Not built** |
| Sub-100ms live detection proof | **Not done** |
| Nested folder flatten | Deferred (large move) |
| Merging all old draft PRs | Deferred |
| 24/7 hosted production proof | Your deploy + multi-day run |

## Run paper dashboard

```bash
python run_dashboard.py
# or
python -m dashboard
```

## Safety

Paper only. See `LIVE_BOUNDARY.md` and `LIVE_READINESS.md` before any future real-fund work.
