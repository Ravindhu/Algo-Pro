# 0027 - Main dashboard

Date: 2026-11-07
Status: ACCEPTED (owner instruction, 2026-11-07).

## Context

Several dashboard pages existed or were drafted in different styles. Agents were
building UI in more than one place, and the flow diagram lived in a separate file.

## Decision

1. The main dashboard is `dashboard/main_dashboard.html`. It is the only dashboard
   page that agents build or change.
2. Its header has a FLOW button. The button opens the Quant System Flow v5 popup.
3. Every agent follows this layout and this file. Do not create a second dashboard page.
4. The page shows P&L by default. EV, MAE/MFE and rule compliance are not shown (AGENTS rule 8).
5. The page is read-only in this version. Its values are static placeholders. Wiring to the
   FastAPI endpoints (`dashboard/app.py`) is a later step with its own decision.

## Done 2026-11-07 (owner approved the route switch)

- `/ui` in `dashboard/app.py` now serves `dashboard/main_dashboard.html`.
- The Trade setup panel was copied from the old page. It reads and saves `/api/setup` with the token.
- The main page is wired to `/api/overview`, `/api/limits`, `/api/kill/trip`, `/api/kill/reset` and `/api/setup`.
  Each write needs the token in the header. The token is kept in memory only.
- The old page `src/quant/index.html` was deleted. Its checks now run on the main page (`tests/unit/test_pnl_visible.py`).

## Open items

- START and STOP are disabled. The paper runner is started on the owner machine.
- The equity curve shows realized P&L from closed trades only (`equity_curve` in `dashboard_data.py`). Open-position P&L is not in the curve, because past prices are not stored.
- The neural shell and its values are a layout only. They are not connected to live data.
- The market chip checks hours only. Exchange holidays are not checked.
- The flow popup is hand-drawn. It is not generated from `quant_system_flow_v5.svg`.
  Connectors (feedback loop, kill switch) are not drawn yet.
