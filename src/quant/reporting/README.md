# src/quant/reporting

Dashboard and accounting views.

- `dashboard.py`: quant dashboard data. Which panels are shown (P&L, EV, MAE/MFE, profit factor, rule compliance) is an OPEN OWNER DECISION. Decision 0007 and AGENTS.md rule 8 disagree. See `docs/QUANT_FOLDER_PLAN.md` section 4.
- `accounting.py`: password-gated accounting view. DONE (lockout tested). Owner to confirm the method (PB-4).

Rule: P&L values never set size or create an order.
Related: `docs/decisions/0007-pnl-shown-by-default.md`, `AGENTS.md` rule 8.
