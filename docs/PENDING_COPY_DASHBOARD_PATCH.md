# Pending Copy Dashboard Wiring

The helper `engine/pending_copy_report.py` is already on this branch.
Apply the following small edits so `/api/paper-simulation` and the UI expose pending orders.

## 1. `dashboard/server.py`

**Import** (near other paper imports):

```python
from engine.pending_copy_report import attach_pending_copy
```

**End of `load_profile_paper_simulation_report`** (before `return base`):

```python
    try:
        attach_pending_copy(base, simulator=get_profile_paper_simulator(profile_id))
    except Exception:
        attach_pending_copy(base, simulator=None)
```

**End of `load_paper_simulation_report`** (global path, before `return base`):

```python
    try:
        attach_pending_copy(base, simulator=get_paper_simulator())
    except Exception:
        attach_pending_copy(base, simulator=None)
```

## 2. `dashboard/index.html`

**Metric** (next to Unrealized P&L):

```html
<div class="metric"><div class="label">Pending Copy Orders</div><p id="pending-copy" class="value">—</p></div>
```

**JS** (where realized/unrealized are set):

```javascript
  const pending=(r.pending_copy&&r.pending_copy.pending_count!=null)?r.pending_copy.pending_count:"—";
  $("pending-copy").textContent=String(pending);
```

## Safety

- Read-only report fields only
- No signing or broadcast
- Paper mode only
