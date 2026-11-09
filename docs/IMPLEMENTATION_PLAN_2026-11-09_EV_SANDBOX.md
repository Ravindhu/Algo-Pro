# Implementation plan: EV on dashboard + SANDBOX panel (2026-11-09)

Flow stage: Dashboard (reporting lane).
Status: done. Decision: `docs/decisions/0033-ev-on-dashboard-and-sandbox-panel.md`.

## Why
- Owner asked to show EV on the dashboard. Owner removed the EV ban from `AGENTS.md` rule 8.
- Owner asked to create the sandbox. The SANDBOX panel had been removed by commit `cfc349e`.

## Scope
- In: EV, average win, average loss, risk-to-reward. SANDBOX panel restored with these stats.
- Out: MAE/MFE and rule compliance (still banned). A standalone sandbox trading engine (needs a flow-stage decision).

## Steps and status
1. Add `expectancy()` to `reporting/dashboard.py`. Done.
2. Carry the new fields in `reporting/dashboard_data.py`. Done.
3. Show EV in the Portfolio panel and the SANDBOX panel. Done.
4. Small-sample note below 30 trades. Done.
5. Update old tests that banned EV. Done.
6. Add EV tests (`src/quant/tests/test_sandbox_ev.py`). Done.
7. Run the full suite. Done. 33 failures were already present before this round and are not caused by this change.

## Exit check
- EV formula equals `win_rate * avg_win + (1 - win_rate) * avg_loss`. Tested.
- EV is `None` with no trades, never `0`. Tested.
- Dashboard JS parses and every element ID it writes exists. Checked.

## Open items
- Pre-existing failures: HRP tests, `test_phase5_wraps`, `test_upgrade_pin`. Not in scope.
- Work-log entry not written: `Temp/Daily work log/` is git-ignored and absent from the clone.

## Next priority
1. Decide if the standalone sandbox trading engine is wanted, and which flow stage owns it.
2. Fix the pre-existing failures before the next release.

## Round 3: full Sandbox page (owner request, 2026-11-09 evening)
- Sandbox = paper trading view inside `dashboard/main_dashboard.html` (no new page, per AGENTS rule 9).
- Pipeline strip shows 12 v5 stages on the paper path. Colours come from `overview.flow`.
- Account block: capital base, paper equity, return on capital, realized, unrealized, max drawdown.
- Statistics: closed trades, win rate, profit factor, EV, EV %, avg win, avg loss, risk:reward.
- Controls and status: mode, paper loop, kill switch, open positions, risk used.
- Closed-trades table (last 20, newest first) uses the existing `closed_trades` field.
- Backend: `sandbox_account()` in `dashboard_data.py` (capital, equity, return %). Shows "not set" when capital is unset.
- Tests: `src/quant/tests/test_sandbox_ev.py` (8 tests, hand-checked numbers). Full suite: 33 failures, all pre-existing.
- Preview: sample paper data built by the real backend code. Not live.
- Not done: standalone sandbox trading engine. Broker feeds stay "not connected" (no live feed).

## Round 4: standard paper trading dashboard (owner request, 2026-11-09)
- Scope changed: no quant-flow pipeline on this view. Standard paper-trading layout.
- Sections: account summary, stats, open positions, closed trades (with return %), order history (last 20 fills).
- Orders are NOT placed from the dashboard. Manual buy/sell would bypass the risk gate (AGENTS: risk gate is the only path to orders). Paper loop and kill switch keep their controls.
- Backend: `fills_recent` (order history) and `ret_pct` on closed trades. Tests in `test_sandbox_ev.py` (10 tests).
- Preview: sample paper data, not live.

## Round 5: SANDBOX zones and stop button (owner request, 2026-11-09)
- Four zones in the SANDBOX popup: header bar, P&L strip, execution quality, active positions. Each has an alert line.
- Placeholders are honest: "not set" / "no data yet" when the system has no value.
- LTP column in open positions (`last_price` from the ledger price feed).
- Stop paper trading button in the SANDBOX popup. Uses the existing paper-loop stop route. No new order path.
- Pending in this round: tax rate module lookup, slippage from OpenAlgo.
- Round 5 part 2 (done): estimated net P&L from `research/costs.py` CostModel (delivery rates, incl. 5 bps impact). Labelled as estimated. STOP PAPER TRADING button in SANDBOX popup calls `/api/loop/stop`.
- Slippage: still open. Needs arrival price logged at order time (risk-gate path). Not built.
