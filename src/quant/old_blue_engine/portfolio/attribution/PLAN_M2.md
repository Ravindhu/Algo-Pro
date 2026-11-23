# M2 — Portfolio Analytics & Tearsheet (Plan + Work Log)

> Track M2 in the portfolio roadmap. Status legend: [x] done, [~] in progress, [ ] pending.

## Scope (from M0 plan)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | `portfolio/attribution/tearsheet.py` — weight history, turnover, drift, HHI, rolling vol/Sharpe | [x] | Pure, degradation-safe, no GUI |
| 2 | `PortfolioManager.get_tearsheet(days)` API | [x] | Wraps tearsheet.build from run rows + drift history |
| 3 | Unit tests `tests/test_tearsheet.py` | [x] | 11 tests, pytest-compatible + self-runner |
| 4 | Dashboard "Portfolio / Tearsheet" tab | [~] | 5 figures: weights, drift, HHI, turnover, gross+rolling vol |
| 5 | Run full test suite green | [ ] | |

## Design decisions

- **Pure core / thin GUI**: `tearsheet.py` does zero dash/plotly imports — only
  returns JSON-safe dicts (`{"ok": bool, ...}`). The Dash layer only renders.
- **Degradation contract**: `build([], [])` returns `{"ok": True, "runs": 0}` —
  never raises, so the dashboard always has something to show.
- **All series carry `ts`** from the drift-history snapshot so every figure is
  time-aligned on the same x-axis.
- **Metrics chosen (practitioner reasons)**:
  - **HHI** (sum of squared weights): 1 = all-in one sleeve; ~0.5 = two-sleeve
    50/50. Shows whether the allocator is actually diversifying over time.
  - **Tracking drift** (half-L1 |target−actual|): how far live positions stray
    from the paper portfolio — the "can we trade what we designed?" gauge.
  - **Planned vs actual turnover**: compares rebalance intent with realized
    churn — a big gap means execution friction (fills, min-qty floors, gaps).
  - **Rolling vol / Sharpe (20 obs)**: risk consistency check on the gross path.

## API

```python
from blue_engine.portfolio import portfolio_manager as pm_mod
pm = pm_mod.PortfolioManager.create({"persist": False})
tearsheet = pm.get_tearsheet(days=7)   # {"ok": bool, "runs": int, "figures-data": {...}}
```

## Test log

```
$ & ..\openalgo\.venv\Scripts\python.exe tests\test_tearsheet.py
PASS test_build_empty_never_raises
...  (fill in after run)
```
