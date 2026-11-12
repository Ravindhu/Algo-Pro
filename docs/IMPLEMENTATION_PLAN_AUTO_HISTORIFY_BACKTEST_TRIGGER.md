# Implementation plan: automatic Historify download, then automatic backtest

Date: 2026-11-12
Status: PLANNED. No code changed.
Depends on: docs/IMPLEMENTATION_PLAN_HISTORIFY_FIRST.md (Phase 0 owner checks done) and the cache and backtest plan.
Flow stage: Market inputs (research data), Research validation, Monitor (session).
Target path in repo: docs/IMPLEMENTATION_PLAN_AUTO_HISTORIFY_BACKTEST_TRIGGER.md

## Goal

Without anyone pressing a button:
1. Historify keeps the screened stocks' daily bars up to date after market close.
2. A freshness check runs on the stored data.
3. If the data passes, the backtest runs on it. If not, the run stops and the report says why.
4. A short report is written. Nothing is promoted or traded.

## Rules this plan keeps

- Research only. No order, no live signal, no change to risk limits.
- Backtest reads the local Historify store (or its export), never the network.
- A failed check stops the chain. It never runs the backtest on partial data.
- Every run is logged to the audit log.
- Broker credentials stay in .env.

## Known limits (from the vendored Historify docs)

- Schedules use the watchlist as their symbol list. Their lookback is 1 to 365 days, so the 5-year backfill is a one-time job, not a schedule.
- Schedules run only while OpenAlgo is running and the broker is logged in.
- Daily broker logins expire, so an unattended run needs a login step first.
- Historify's schedule timing and misfire behaviour on our install are not yet tested.

## Phase 0: owner checks

- Broker logged in daily (automatic login step decided: manual, or a scripted login that uses the existing broker auth).
- Machine stays on from before 3:30 PM IST until the run finishes (set power settings).
- Decide the time: default 3:35 PM IST (after market close), with a fallback retry at 6:00 PM IST.
- Exit check: owner confirms these before any schedule is turned on.

## Phase 1: one-time backfill

File: scripts/research/historify_backfill.py

- Creates the watchlist from config/research_universe.yaml (or the screened list when available).
- Starts one job: interval D, last 5 years, incremental true.
- Polls until completed. Prints counts, not data.
- Exit checks:
  - A failed symbol is reported and does not stop the others.
  - Re-running is safe: incremental mode skips covered dates.

## Phase 2: daily schedule

- Create one Historify schedule: daily, time from config (default 15:35 IST), watchlist as the source, lookback 5 days (covers weekend and holiday gaps).
- Exit checks:
  - Schedule can be paused and resumed from the dashboard (existing routes).
  - If OpenAlgo or the broker is down at run time, the execution history records a failure. No silent success.

## Phase 3: freshness check (gate)

File: src/quant/data/freshness.py

For each symbol in the watchlist, after the schedule's execution finishes:
- Last stored date equals the last trading day in the NSE holiday calendar (config/calendar/holidays.csv).
- No missing trading days in the last 5 days.
- No zero or negative prices, High/Low/Close consistent (same checks as the QA gate).
- Row count for the window is not below the previous run's count.

Result: PASS, or FAIL with a list of symbols and reasons. A FAIL stops the chain.

Exit checks:
- Planted missing day makes the gate FAIL.
- Stale data (last date older than the last trading day) makes the gate FAIL.
- Test: the gate never reads the broker, only the store.

## Phase 4: backtest trigger

File: scripts/research/run_daily_research.py

Runs only after a PASS from Phase 3, in this order:
1. Export the window from Historify to the research cache (Parquet), with a checksum.
2. Data QA (existing gate).
3. Backtest from the cache (costs, walk-forward, DSR and PBO).
4. Optimizer only if the owner turned it on in config. Every trial goes to the trial log.
5. Feedback report (advice only, no promote()).

Each step writes to the audit log with a run id, input checksum and result.

Exit checks:
- A FAIL at Phase 3 means no backtest starts (test).
- The same input checksum gives the same result (determinism test).
- Nothing in this chain imports the order path (AST test).

## Phase 5: monitoring and alerts

- Each run writes a short status: PASS or FAIL, symbols affected, backtest result summary, run time.
- Status shows on the Market inputs node in the flow popup (amber on FAIL, red if the run crashed).
- Alerts follow the existing levels: FAIL is WARNING, crash is ERROR. None of them trips the kill switch.

Exit checks:
- A planted FAIL shows amber on the node.
- A killed run shows red after the next poll.

## Phase 6: scheduling the chain

- One scheduler entry runs the Historify schedule, then waits for its execution to finish, then runs Phase 3 and Phase 4.
- Pick one of: the existing OS scheduler (scripts/ops/install_schedule.py), or a Historify execution callback. The owner decides after testing which one is reliable on the owner's machine.
- Exit checks:
  - Run twice in one day: second run is a no-op (incremental).
  - Machine restarted mid-run: next run recovers, no duplicate data.

## Owner decisions

1. Daily login: manual each morning, or a scripted login?
2. Run time (default 15:35 IST) and retry time (default 18:00 IST).
3. Watchlist source: fixed list in config, or the screened list once the screener writer exists.
4. Optimizer: on or off by default for the daily run.
5. Machine uptime: will the owner's machine be on every trading day?

## Cost and time

- Backfill: one-time, see the Historify plan. Broker cost depends on the broker.
- Daily run: minutes, no network after the download, no model tokens.

## Not in this plan

- Live trading, live signals, risk-limit changes, promotion of any model.
- Running this on the live server or on the NSE MCP live data.

## Repo note

This session cannot push to Ravindhu/Algo-Pro. The owner adds this file, or grants write access.
