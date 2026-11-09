# src/quant/reporting

Dashboard and accounting views.

- `dashboard.py`: quant dashboard data. Shows P&L, win rate, profit factor and risk use. EV is shown (decision 0033). MAE/MFE and rule compliance are removed (decision 0008). MAE/MFE feed the optimizer instead.
- `accounting.py`: password-gated accounting view. DONE (lockout tested). Owner to confirm the method (PB-4).

Rule: P&L values never set size or create an order.
Related: `docs/decisions/0007-pnl-shown-by-default.md`, `docs/decisions/0008-dashboard-metrics-and-optimizer-excursion-weights.md`, `AGENTS.md` rule 8.
