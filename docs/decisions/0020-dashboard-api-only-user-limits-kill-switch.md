# Decision 0020: Dashboard API-only, user-set limits, kill switch closes positions

Date: 2026-11-06
Status: APPROVED by owner (chat, 2026-11-06, two rounds). Answers below are final.
Implemented in: 8fd0d4f (kill close), c8b29a0 (risk_used), and the commit that adds this line (items 1-6 below).

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

## Round 4 (2026-11-07): kill switch button and kill routes

- Owner request: a kill switch button on the page, and a dashboard API. This reverses the
  round 1 reading of "works in background only" for the page: the page can now trip and reset the switch.
  Owner to confirm this reading.
- Routes (same token rules as limits: header `X-Dashboard-Token`; 503 no server token; 401 wrong token):
  - `GET /api/kill`: read `active` and `reason`. No token.
  - `POST /api/kill/trip` with `{"reason": "..."}`: sets the switch, writes a `kill_switch` audit record.
  - `POST /api/kill/reset` with `{"reason": "..."}`: clears the switch, writes an override record (`kill_switch_reset`).
  - Reason: required, 1 to 200 characters. Missing or bad reason is 422.
- Trip does NOT close positions. The dashboard sends no orders (AGENTS rule 2). The paper runner
  closes open positions on its next run (`_flatten_if_killed`). Gap: no supervised loop runs it
  between runs, so an open position stays open until the next run. Owner decision needed: accept this,
  or build the supervised loop first.
- Code fix: `dashboard/app.py` no longer uses `from __future__ import annotations`. FastAPI reads route
  annotations from module globals, and `Request` is local to `create_app`.
- Code fix: the token check compares UTF-8 bytes, so a non-ASCII header no longer raises.
- Tests: `tests/unit/test_kill_switch_store.py` runs in the sandbox. Kill route tests in
  `tests/unit/test_dashboard_app.py` need FastAPI (owner run).

## Round 3 (2026-11-06 late): page wired to the backend

- The page is served at `/ui` (FastAPI). `/` stays the JSON index, as in item 1 of round 1.
  The flow asset is served at `/assets/flow_svg.js`, so the page works from the app, not from disk.
- Read: every field of `/api/overview` is shown (mode, kill switch, feed, P&L, performance,
  positions, events, insight, limits). Polls every 2 s (AGENTS: not below 500 ms).
- Write: the limits form posts only changed fields to `POST /api/limits`. Empty = null (not set).
- Token: typed into the page, sent as `X-Dashboard-Token`, kept in JavaScript memory only.
  Not written to localStorage, sessionStorage or cookies (a test checks this).
- Not on the page, on purpose: kill switch control (owner: background only) and sizing settings
  (`risk_per_trade` is not in the API).
- Not run here: no FastAPI or browser in the sandbox. Owner must open `/ui` once and test a save.

## Owner answers (2026-11-06, second round)

1. Yes: move `dashboard/index.html` to `src/quant/index.html`; remove its simulated engine.
   Done: the new page is a static flow diagram that loads `dashboard/assets/flow_svg.js`. It shows no orders.
2. Yes: risk per trade 0.25% of equity. Setting: `config/settings.yaml` `sizing.risk_per_trade`.
   Helper: `risk_budget_qty` in `src/quant/portfolio/sizing.py`.
   Gap: the paper runner takes qty from the signals file. The signal stage that would use this is not built yet.
3. Yes: user-set limits through `POST /api/limits`. Needs header `X-Dashboard-Token` = env `DASHBOARD_API_TOKEN`.
   No token set = writes refused (503). Bad token = 401. Bad value = 422. Each save writes a `limits_changed` audit record.
   Saved YAML keeps the header comment; inline comments are not kept.
4. Yes: automatic kill on a daily realized-loss breach of `max_daily_loss`. Unrealized losses do not trip it.
   Limit: "daily" is the whole ledger file for one paper run (fills have no date yet).
5. Yes: a kill close passes halt-all. Only while the kill switch is on.
6. "Kill switch should block both": wording now reads "Blocks entries, closes all" (SVG, flow asset, flow doc, AGENTS rule 6).
   Reading used: it blocks new entries AND closes all open positions. Closing is the only exit it sends.
   It does NOT block normal exits (stop, time, manual) from other stages, because blocking those would leave positions open.
   If "both" meant blocking those exits too, this needs a new decision.
   The C2PA signature on `quant_system_flow_v5.svg` is now invalid (owner accepted this).
