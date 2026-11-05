# 0008. Dashboard shows P&L; EV, MAE/MFE and rule compliance removed; MAE/MFE become optimizer parameters

Date: 2026-11-05
Status: Accepted (owner decision)
Supersedes: the EV, MAE/MFE and rule-compliance parts of the dashboard rule in `AGENTS.md` rule 8 and the dashboard panel list in `docs/PRIORITY_IMPLEMENTATION_PLAN.md`. Decision 0007 (P&L shown) stays in force.

## Context

Three sources disagreed on the dashboard (see `docs/QUANT_FOLDER_PLAN.md`, section 4). The owner resolved it:
1. P&L shown by default.
2. EV, MAE/MFE removed from the dashboard.
3. MAE/MFE used as optimizer parameters.

## Decision

Dashboard (`src/quant/reporting/dashboard.py`, `dashboard/index.html`):
- Shown: total P&L (toggle can hide it), win rate, profit factor, risk utilisation, trade count.
- Removed: expectancy / EV per trade, average MAE, average MFE, rule-breach count and rule-compliance panel.
- Pass-through: `build_payload(trades, risk_used)`. The `rule_breaches` argument is gone.
- The web dashboard's "Expectancy / trade" card and its update line are removed.

Optimizer (`Blue_engine/optimizer/objective.py`, `engine.py`):
- `Objective` and `EngineConfig` take `mae_penalty_per_pct` and `mfe_capture_weight` as parameters. Both must be finite and >= 0.
- Default values are unchanged (0.02 and 0.1). They are still UNTESTED.
- The score change is computed by one pure function, `excursion_adjustment`.
- Both weights are written to `Objective.stats()` and to the trial-log run row, so each run records the values it used.
- Automatic search over the two weights is NOT on. It needs approved search bounds and out-of-sample checks first (see Open below).

Diagrams and docs: `quant_system_flow_v5.svg`, `dashboard/assets/flow_svg.js`, `docs/quant_system_flow_v5.md`, `AGENTS.md` rule 8 are updated to match.

## Why

- Removing EV and MAE/MFE from the screen follows the owner's focus on P&L.
- The MAE/MFE data is still needed for research. Moving it into the optimizer keeps it in use without showing it during live trading.

## Open

- Search bounds for the two weights, and an out-of-sample test that shows they help. Needed before they are searched automatically.
- Trial logging is off by default (`EngineConfig.trial_log_path = None`) so test runs do not write to the research log. The research runner must set the path (AGENTS: every run must be logged).

## Tests

- `tests/unit/test_dashboard_metrics_handchecked.py`: removed keys are absent from the payload; P&L, win rate and profit factor are checked by hand.
- `tests/unit/test_excursion_weights.py`: `excursion_adjustment` hand examples; weights stored, reported and validated.
- `tests/unit/test_phase3.py`: dashboard test renamed and changed to the new rule. It needs `openalgo` for profit factor, so it cannot run in the sandbox.
