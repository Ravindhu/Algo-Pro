# FastAPI Dashboard Plan

Status: D1-D4 DONE; D5 (commit and push) in progress (2026-11-05)
Owner request: "I need Make fast api ai dashboard".
Rules: AGENTS.md "Use Fast api dashboard" (dashboard section), rule 8 (P&L shown by default; no EV, MAE/MFE, rule compliance), and the error-monitoring section.

## Plain-English goal
A read-only web page, served by FastAPI, that shows the paper system state:
mode, kill switch, safe mode, risk limits, positions, realized P&L, win rate,
profit factor, risk use, safety events, and an "AI insight" line per panel.
The dashboard never sends orders. It only reads files.

## Design
- Data logic lives in `src/quant/reporting/dashboard_data.py` (pure Python, testable without FastAPI).
- `dashboard/app.py` is a thin FastAPI wrapper. It imports FastAPI only when it runs.
- Page: `dashboard/assets/terminal.html` (plain HTML + JS, refreshes every 2 s, no CDN).
- The existing `dashboard/index.html` (flow diagram page) is kept.
- AI insight: rule-based sentences generated from the numbers. No LLM call, no trade path.
  Labelled "advice only" on screen.
- P&L visibility: the API always returns P&L. The page starts with P&L shown or hidden,
  based on `config/settings.yaml` key `dashboard.pnl_visible_default`. Default: true (AGENTS rule 8). Owner decision D8 still open.

## Phases
- D1 Data module (`dashboard_data.py`) + tests. DONE: FIFO realized P&L, positions, safety events, AI insight, no EV/MAE/MFE.
- D2 FastAPI app (`dashboard/app.py`) with `/`, `/api/overview`, `/api/positions`, `/api/events`, `/api/health`. DONE. Route tests run only where FastAPI is installed (not in this sandbox).
- D3 Terminal page (`dashboard/assets/terminal.html`). DONE: refreshes every 2 s, P&L toggle.
- D4 Docs: SOP section 20, work log. DONE.
- D5 Commit and push. Verify remote head.

## Not in this pass
- Live feed health (no live feed exists yet, so `/api/health` reports "not connected").
- Order or kill-switch controls from the browser (read-only by design).
- Dash/Plotly version (AGENTS mentions both Dash and FastAPI; the owner asked for FastAPI).

## Work log
- 2026-11-05: plan written. D1-D4 built. Tests: 248 passed, 70 failed (same openalgo set), 36 skipped.
- Not verified here: FastAPI routes and the page in a browser (FastAPI not installed in the sandbox). Owner must run `python dashboard/app.py` and open http://127.0.0.1:8050.
