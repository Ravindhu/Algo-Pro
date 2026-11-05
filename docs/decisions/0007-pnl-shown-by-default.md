# 0007. P&L is shown by default on all dashboards

Date: 2026-11-05
Status: Accepted (owner decision)
Partly superseded by 0008: EV, MAE/MFE and rule compliance are removed from the dashboard. The P&L part stands.
Supersedes: the P&L-hidden-by-default part of 0006 and the "P&L hidden by default" rule in `AGENTS.md` (rule 8) and `docs/quant_system_flow_v5.md`.

## Context

Earlier work hid daily and unrealised P&L by default, to reduce emotional trading (Priority B).
The owner reviewed this and asked that P&L show on the dashboard.

## Decision

- Terminal dashboard (`Blue_engine/dashboard/render.py`): `reveal_pnl` defaults to `True`.
- Web dashboard (`dashboard/index.html`): starts with P&L visible. The button reads "Hide ₹".
- Quant dashboard (`src/quant/reporting/dashboard.py`): already shows total P&L (commit `f3e28da`). No change.
- A toggle still hides money figures on screen. Callers can pass `reveal_pnl=False`.
- The password-gated accounting view (`src/quant/reporting/accounting.py`) is unchanged.

## What does not change

- P&L can never set position size or create an order.
- Profit factor stays visible. EV, MAE/MFE and rule compliance were removed by decision 0008.
- Risk limits and kill switches work the same way.

## Tests

- `tests/unit/test_pnl_hidden.py`: shown by default, hidden when the toggle is off, web default is shown.
- `tests/unit/test_phase3.py`: `test_dashboard_shows_pnl_and_hides_ev_mae_mfe` needs `openalgo` in this sandbox. It is one of the known pre-existing failures.

## Consequence to review

If behaviour on a live screen causes problems, re-open this decision. Changing the default is a one-line change in each file listed above.
