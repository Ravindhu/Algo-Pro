# Implementation plan: Sandbox section on the NSE dashboard (2026-11-09)

Flow stage: Dashboard (reporting / notify lane).
Status: in progress.

## Goal
Add a **SANDBOX** section to `dashboard/main_dashboard.html`. It shows paper-trading statistics in one place.

## Scope
- Frontend only. One file changed: `dashboard/main_dashboard.html`.
- Reads only existing fields from `/api/overview`. No new endpoint. No orders. No broker calls.
- All numbers are simulated (paper ledger). They are not live market results.

## Discovery done
- `dashboard/main_dashboard.html`: the one dashboard page (decision 0027).
- `dashboard/app.py`: `/api/overview` already returns performance, equity, positions, loop and kill-switch state.
- `AGENTS.md`: dashboard UI only in `main_dashboard.html`; P&L is the default view.

## Steps
1. Add the SANDBOX panel between the top row and the middle row.
2. Fill the panel in `render()` from fields already in the overview.
3. Keep masked money (`••••`) behaviour for all rupee values.
4. Run `pytest` (not run in this session if the sandbox cannot install packages).
5. Append the work-log entry under `Temp/Daily work log/` after the commit.

## Rule conflicts found (need owner decision)
- `AGENTS.md` rule 8 says EV is removed from the dashboard.
- The user preference file asks for EV on the trading interface.
- **Not added.** EV needs average win and average loss, which the overview does not return yet.

## Exit check
- Panel shows `—` when the API is offline.
- Panel values match the existing Portfolio panel values.
- No new write route. No change to `app.py`.

## Next priority
1. Decide the EV conflict (AGENTS.md rule 8 vs user preference).
