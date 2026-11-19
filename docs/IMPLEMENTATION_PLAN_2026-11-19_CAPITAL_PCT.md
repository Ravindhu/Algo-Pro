# Plan: loss caps as % of capital, set from Trade setup (2026-11-19)

Status: DONE (2026-11-19). Paper only. Tests: related suites pass; full suite has no new failures vs untouched code.

Owner request (2026-11-19): capital base is ₹10,00,000 (paper). Max daily loss = 2 % of capital.
Max strategy loss = 1.5 % of capital. The two percentages are set from the dashboard Trade setup popup.

## Design (simple words)
- The percentages and the capital base live in `config/settings.yaml` under `risk_budget`.
  (`RiskLimits.from_yaml` rejects unknown keys in `risk_limits.yaml`, so they cannot go there.)
- The rupee caps the gate reads (`max_daily_loss`, `max_strategy_loss` in `config/risk_limits.yaml`)
  are derived: rupees = percent x capital. The save path writes both files together.
- `/api/limits` refuses these two rupee keys, so the old limits form cannot make them drift.
- Gate code is unchanged: it still reads rupee values.

Derived values now: daily loss ₹20,000 (2 %), strategy loss ₹15,000 (1.5 %).

## Phases
| Phase | Work | Tests |
|---|---|---|
| 1 | setup_store: budget keys, validation, derived rupee write, read | unit: save, derive, refusals |
| 2 | app.py: audit limits_changed for the rupee caps; /api/limits refuses them | API test if test harness exists |
| 3 | dashboard Trade setup popup: 3 fields + live ₹ hint; limits form ₹ fields read-only | manual check of page script |
| 4 | Config values set (settings.yaml, risk_limits.yaml) | load test |

## Notes
- Changing `risk_limits.yaml` voids the live approval (hash check). Paper only now; no live effect.
- Hand edits to the rupee caps can drift from the percentages. The save path is the supported route.
