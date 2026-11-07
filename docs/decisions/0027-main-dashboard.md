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

## Open items (not done in this commit)

- `dashboard/app.py` still serves the older page `src/quant/index.html` at `/ui`. Switching
  `/ui` to `main_dashboard.html` is a route change. It needs owner approval.
- The flow popup is hand-drawn. It is not generated from `quant_system_flow_v5.svg`.
  Connectors (feedback loop, kill switch) are not drawn yet.
