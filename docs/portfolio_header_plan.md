# Portfolio header: plan and status

Flow stage: Dashboard (read-only holdings via OpenAlgo). Decision: docs/decisions/0032-readonly-holdings-openalgo.md

## Goal
Show the owner's real demat holdings in a Portfolio header on `dashboard/main_dashboard.html`.

## Steps
1. [done] Discovery: `dashboard/app.py`, `main_dashboard.html`, `openalgo_client.py`, OpenAlgo holdings route in `src/quant/openalgo_platform/`.
2. [done] Owner approval: read-only, Algo-Pro, holdings from logged-in OpenAlgo.
3. [done] `OpenAlgoClient.get_holdings` (read only).
4. [done] `reporting/portfolio_view.py`: shaping, states, 30 s cache, 50-row cap.
5. [done] `GET /api/portfolio` in `dashboard/app.py`.
6. [done] Portfolio header in `main_dashboard.html` (30 s refresh, ₹ toggle).
7. [done] Tests: `src/quant/tests/test_portfolio_view.py` (9 cases); page-route test updated.
8. [pending] Live check on the owner's machine with OpenAlgo logged in (cannot run from the build workspace).

## Exit checks
- Full suite passes, except the known `test_upgrade_pin` failure that predates this work.
- No order call reachable from the holdings path (test asserts the URL is `/api/v1/holdings`).
- Secrets never appear in responses or errors.
