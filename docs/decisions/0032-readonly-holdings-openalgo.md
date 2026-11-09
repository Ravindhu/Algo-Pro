# 0032 - Read-only broker holdings on the dashboard (Portfolio header)

Date: 2026-11-09
Status: ACCEPTED (owner answers 2026-11-09: read only; repo Algo-Pro; holdings from the owner's logged-in OpenAlgo)

## Context

The NSE dashboard had no view of what the owner's demat account holds. AGENTS.md says nothing in
Phases 1-2 may reach OpenAlgo, so a holdings read needed an explicit owner decision.

## Decision

1. A new read-only call `OpenAlgoClient.get_holdings(api_key)` posts `{"apikey": ...}` to
   `/api/v1/holdings`. It sends no order. It lives in `execution/openalgo_client.py`, the only module that talks to OpenAlgo.
2. `reporting/portfolio_view.py` shapes the reply. It keeps broker numbers as they are, leaves
   missing values as None, and never estimates. States: `broker_read`, `not_connected`, `error`.
3. `GET /api/portfolio` in `dashboard/app.py` serves the header. Results are cached for 30 s,
   and errors are cached too, so a stopped OpenAlgo is not hammered.
4. The header in `dashboard/main_dashboard.html` refreshes every 30 s, is read-only, and has no order
   buttons. Money follows the existing ₹ show/hide toggle.
5. The app builds the client with `mode="analyze"`. The mode is not used by `get_holdings`, which sends no order.
   The reader gets only `get_holdings` and the key loader, so no order method is reachable from it.
6. The API key is read from `.env` (`OPENALGO_API_KEY`) and is never returned, logged, or echoed in an error.
   The base URL comes from `OPENALGO_URL` (default `http://127.0.0.1:5000`).

## Consequences

- Real holdings appear only when the dashboard runs on the owner's machine with OpenAlgo logged in to the broker.
- The page route test (`tests/unit/test_pnl_visible.py`) now lists `/api/portfolio` as a read route.
- The `test_upgrade_pin` failure predates this change (it fails on a clean tree) and is not fixed here.
