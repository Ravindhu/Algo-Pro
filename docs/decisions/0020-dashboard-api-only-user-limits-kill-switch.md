# Decision 0020: Dashboard API-only, user-set limits, kill switch closes positions

Date: 2026-11-06
Status: APPROVED by owner (chat, 2026-11-06). Code not yet changed.

## Context

Two dashboard pages exist. `dashboard/assets/terminal.html` is served by `dashboard/app.py`.
`dashboard/index.html` is a separate page with its own simulated trading engine in browser JavaScript.
The simulated engine places fake orders, uses random prices, and hard-codes risk limits.
This conflicts with AGENTS.md: one dashboard only, no parallel order path, and limits come from
`config/risk_limits.yaml`, which is null on purpose.

## Decision

1. `/` serves the JSON API only. `dashboard/assets/terminal.html` is DELETED, not moved to Temp (owner instruction).
2. Endpoints kept: `/api/overview`, `/api/positions`, `/api/events`, `/api/health`.
3. Risk limits are USER-DEFINED. A new authenticated write endpoint saves them to `config/risk_limits.yaml`.
   Unset limits stay null, so entries fail closed. Every change writes an audit record (old and new values).
4. Risk per trade = 0.25% of equity (was 0.5% in the simulation).
5. Kill switch: when it fires it CLOSES ALL OPEN POSITIONS and blocks new entries.
   Interpretation to confirm with owner: "works in background only" means the kill-switch logic runs in the
   Python backend (risk gate and kill-switch store). It is not a button on the page; the page only shows its state.
6. `dashboard/index.html` MOVES to `src/quant/index.html`. Its simulated engine is removed, so it does not place orders.
   Its flow diagram is kept and rendered from the SVG.
7. The flow diagram, `docs/quant_system_flow_v5.md`, and AGENTS.md rule 8 change the kill-switch wording to
   "closes all open positions and blocks new entries". The SVG change must be made in the same commit.

## Reasons

- One dashboard, one source of truth: the backend. The page cannot invent prices or orders.
- Limits are a human decision (AGENTS.md). The page must not set them.
- A kill switch that only blocks entries leaves open risk running. The owner wants open risk closed.

## Consequences

- `tests/unit/test_pnl_hidden.py` reads `dashboard/index.html` and must be updated for the new path.
- `tests/unit/test_dashboard_app.py` builds its own page file and is not affected by deleting `terminal.html`.
- The risk-per-trade default lives in config, not in page code.
- Cannot be tested in the sandbox (`httpx`, `openalgo` missing). Owner test run required before live use.
- The SVG contains a C2PA manifest. Editing it removes that signature. Owner accepted this trade-off only if confirmed.

## Open

- Confirm the kill-switch reading (item 5).
- Confirm the SVG edit is acceptable (item 7).
