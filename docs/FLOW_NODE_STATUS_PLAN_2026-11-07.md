# Flow popup: live node status (plan)

Date: 2026-11-07. Owner request: the FLOW header popup must show each node of the v5 flow.
A node with an error must blink red.

Flow stage: Dashboard (reporting + dashboard page). No new stage, no order path touched.
Decision: `docs/decisions/0030-flow-popup-live-status.md`.

## Facts found (discovery)

- `quant_system_flow_v5.svg` boxes are wrapped by `scripts/ops/make_flow_svg.py` into `<g class="stg" data-label=...>`.
- `dashboard/assets/flow_svg.js` is the generated copy. The popup injects it into `#flowbox`.
- `/api/overview` is polled every 2 s. It already carries kill switch, mode, loop, feed, market and limits.
- `tests/unit/test_pnl_visible.py` checks the page's exact POST and route set. So no new route is added.
  Node status rides inside `/api/overview` as `flow`.
- FastAPI and pytest are not installed in this session, so the API test skips here. Node syntax check runs.

## Node rules (current state only, no history)

| Node (data-label) | Red (blink) when | Amber when |
|---|---|---|
| Kill switch | kill switch active | - |
| Safe mode controller | mode is halt-all | mode is reduce-only |
| Paper trading stage | loop has a last_error | - |
| Trading calendar | - | holiday calendar not verified |
| Broker feeds, Feed health monitor | - | feed not connected (always, until live feed) |
| Pre-trade checks | - | max order qty or notional not set |
| Liquidity cap | - | max share of ADV not set |
| Loss budget breaker | - | max daily or strategy loss not set |
| Portfolio risk | - | max open exposure not set |
| Risk gate | - | max symbols per portfolio not set |
| Dashboard | - | - (red only when the browser cannot reach `/api/overview`) |

Old audit events are NOT used for colour. A kill switch reset must clear the red at once.

## Phases

- **Phase A (done):** discovery above.
- **Phase B (backend):** `src/quant/reporting/flow_status.py` pure function `node_statuses(overview)`.
  Wire it in `dashboard/app.py` `load_state` as `overview["flow"]`. Unit tests in `tests/unit/test_flow_status.py`.
- **Phase C (popup):** `dashboard/main_dashboard.html` paints each `g.stg` by `data-label`.
  Red nodes blink with CSS. Amber nodes get an amber border. A tooltip shows the message.
  A list under the diagram shows every non-ok node. Offline API marks `Dashboard` red.
- **Phase D (docs + tests):** decision 0030, this plan, update the old "static diagram" docstring in `test_pnl_visible.py`.
- **Phase E (verify + commit):** Node syntax check, Python compile, plain-Python checks of the rules,
  unit tests where pytest exists. Commit and push only after checks pass.

## Exit checks

1. Clean state gives no red and no amber from the backend, except the feed nodes (feed is not connected).
2. Every label the backend can emit exists as a box label in `quant_system_flow_v5.svg` (test).
3. Page route set and POST count unchanged (existing test passes).
4. Page script parses under `node --check`.

## Not done here (next priorities)

1. Install FastAPI and httpx on the owner machine so the FastAPI overview test and the HRP tests run (not possible in this session).
2. Real feed. Until then the feed nodes stay amber by design.
3. Per-node error events with component ids (AGENTS error-monitoring fields) so more nodes can turn red from real faults.

## Work log

- 2026-11-07: Phase A done. Phases B-D done (flow_status.py, app.py wiring, popup paint and blink, tests, decision 0030).
  Checks: node --check on the page scripts ok; pure flow tests 9/9; full `pytest tests/unit` = 816 passed,
  27 failed, the same 27 as the untouched commit (missing `httpx` in this sandbox: HRP tests).
  The FastAPI overview test skips here (fastapi not installed). Phase E: commit and push.
