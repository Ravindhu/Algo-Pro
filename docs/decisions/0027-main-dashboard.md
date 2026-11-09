# 0027 - Main dashboard

Date: 2026-11-07
Status: ACCEPTED (owner instruction, 2026-11-07).

## Context

Several dashboard pages existed or were drafted in different styles. Agents were
building UI in more than one place, and the flow diagram lived in a separate file.

## Decision

1. The main dashboard is `dashboard/main_dashboard.html`, served at `/ui`. It is the only dashboard
   page that agents build or change.
2. Its header has a FLOW button. The button opens the Quant System Flow v5 popup. It also has PAPER and LIVE
   mode buttons (decision 0028).
3. Every agent follows this layout and this file. Do not create a second dashboard page.
4. The page shows P&L by default. EV, MAE/MFE and rule compliance are not shown (AGENTS rule 8).
5. Writes go only through token-protected routes in `dashboard/app.py`, each audited.

## Done (2026-11-07)

- `/ui` serves `dashboard/main_dashboard.html`. The old `src/quant/index.html` is deleted.
- The page reads `/api/overview` and writes limits, kill switch, trade setup and mode.
- Realized equity curve (`equity_curve` in `dashboard_data.py`), from closed trades only.

## Open items

- START and STOP run the paper loop (decision 0029). The loop has no signal source yet.
- The neural shell is a layout only. The screener has no data yet.
- The market status uses the holiday list in `config/calendar/holidays.csv` (decision 0029).
- The flow popup shows the real diagram from `quant_system_flow_v5.svg`. The page loads `/assets/flow_svg.js`, which `scripts/ops/make_flow_svg.py` generates from the SVG. Regenerate it after editing the SVG.
