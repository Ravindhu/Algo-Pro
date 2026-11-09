# Remove the Blue_engine terminal dashboard

Status: DONE in code (2026-11-05). Commit and push in progress.
Owner request: "Remove the terminal dashboard too" (after: "Blue engine dash plotly dashboard remove from repo").
Rule: AGENTS.md "Never delete unwanted files. Move them to Temp/." Removed code is copied to
`Temp/archive/2026-11-05/` (git-ignored) and stays in git history.

## What is removed
- `Blue_engine/dashboard/` (rich terminal console: app, cli, render, data_source, state, pov_state_machine, candlestick, position_tracker).
- `Blue_engine/input/dashboard.example.json` (config for that console).
- `Blue_engine/tests/test_token_usage_handoff.py` (tests the removed file bridge).
- `Blue_engine/Dockerfile` (its only job was to run the terminal console).

## What is kept, moved, or changed
| Need | Replacement |
|---|---|
| Optimizer watchlist (`resolve_watchlist`) | Moved, unchanged, to `Blue_engine/screener/watchlist.py` |
| Pipeline "publish shift / token usage" to the console | Publish calls removed. Token usage still stored in `report["llm_tokens"]`. Best-effort handoff had no reader after removal. |
| Infrastructure health check ("dashboard") | Checks that FastAPI is importable and the dashboard port is open. |
| Smoke test "dashboard module imports" | Checks FastAPI importable and `dashboard/app.py` present. |
| VPS service `pov-dash` (`Servers/install_ubuntu.sh`) | Runs the FastAPI app with uvicorn on the same port. nginx `/pov-dash/` route unchanged. |
| Installers (`install.bat`, `START_ALL.cmd`, `requirements.txt`) | `[dashboard]` extra dropped. `fastapi` and `uvicorn` added. `plotly` kept for optimizer charts. `rich` dropped. |
| Optimizer plots (`optimizer/plots.py`, Plotly) | Kept. Extra renamed `charts` (plotly, pandas, numpy). |
| `quant/execution/router.py` and `reporting/dashboard.py` docstrings | Text updated (they referenced the removed files). |
| `tests/unit/test_pnl_hidden.py` | Terminal tests removed (they needed `rich` and the removed module). Web-page tests kept. |

## Not changed (on purpose)
- `dashboard/index.html`: the existing web P&L page. It is not part of Blue_engine and its tests stay.
- `patches/openalgo-custom.patch`: upstream OpenAlgo patch text. Left as is.
- Historical SOP sections and dated plan docs: left as written. A note is added where the removal matters.

## Owner risk notes
- The VPS service changes on the next `install_ubuntu.sh` run. The old console will stop.
- No Docker compose file exists in this repo. If one exists outside, its `pov-dash` service must be updated.
- Not run here: the Blue_engine suite (needs `openalgo`), the VPS installer, and Docker. Verify on the owner machine.

## Phases
A. Plan (this file). DONE.
B. Copy removed files to Temp, then `git rm` them. DONE.
C. Extract `resolve_watchlist`; update optimizer import. DONE.
D. Update pipeline, infrastructure, smoke test. DONE.
E. Update packaging, installers, service unit, requirements. DONE (batch files kept CRLF).
F. Update tests and docs. DONE (new tests/unit/test_watchlist.py).
G. Verify: unit 252 passed, 70 failed (same openalgo set, zero new), Blue collect 176, changed Python compiles, bash -n ok, leftover refs only in AGENTS.md rule and an old plan doc.
H. Commit and push; verify remote head.

## Work log
- 2026-11-05: plan written. Phases B-G done.
- Known gaps left alone: AGENTS.md line 310 still describes pov_state_machine.py; PORTFOLIO_ENGINE_PLAN.md still names the removed page; blue_engine/screener/presets.py missing (pre-existing).
