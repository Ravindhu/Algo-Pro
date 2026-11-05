# Implementation workflow: start to end, in quant flow order

Status: created 2026-11-05. Planning doc only. No code changed.
Companion to `docs/IMPLEMENTATION_PLAN.md` (item status and exit checks) and
`docs/quant_system_flow_v5.md` (stage design). Item IDs (P0-*, P1-*, X-*, ...) point to the plan.

Plain English: build the system in the same order that money would flow through it.
Each step must pass its gate before the next step starts. Nothing reaches OpenAlgo
before Phase 3, and Phase 3 stays off until the owner signs off.

---

## Step 0 - Owner decisions and test baseline (no capital)

Work: P0-2, P0-3, P0-4. Also X-23 (holiday check) and X-22 (delete stale branches).
Gate: `pytest tests/unit Blue_engine/tests` runs on the owner machine with `openalgo` installed. Pass and fail counts recorded in `docs/STATUS_*.md`.
Why first: 70 unit tests cannot run in the sandbox. Without a baseline, no later change can be proven safe.

## Step 1 - Research validation (how we know an idea is real)

Flow lane: research validation.
Work, in order:
1. X-5 cost model tests (STT, brokerage, exchange charges, GST, stamp duty, impact).
2. X-4 point-in-time test: features and conviction read only past bars.
3. P1-7 / X-13 survivorship-free universe and corporate actions (`data/universe.py`, `data/corp_actions.py`).
4. P1-6 / X-10 real-data backtest with costs and walk-forward (`research/backtest.py`, `research/walk_forward.py`).
5. X-12 trial log entry for every run (`research/trial_log.py`). No trial log entry, no evidence.
6. DSR and PBO (`research/validation.py`).
Gate (P1 exit checks 3 and 4): costs included, look-ahead test passes, DSR and PBO reported out of sample.
Blocked by: NSE bar data from owner (sandbox cannot reach NSE).

## Step 2 - Data and features (inputs must be clean)

Flow lane: data and features, market inputs.
Work, in order:
1. `data/qa.py`: stale and missing bar gate. Failure stops the run (flow doc 2.1).
2. `data/feature_store.py`: point-in-time values only.
3. `data/market_inputs.py`: finish flow data, bulk/block deals (P2-8). Event parser and blackout dates already DONE.
4. `data/ingest.py`: broker live feed (P3-1). Stays PENDING until Phase 3.
Gate: QA gate blocks bad data in a test. Market inputs are tagged `Live`, `Delayed`, `Historical`, or `Simulated` (AGENTS rule).

## Step 3 - Models and signal (advice and direction only)

Flow lane: signal and conviction, models.
Work, in order:
1. `models/regime.py` (P2-4 / X-18). Validate on Step 1 data first.
2. `models/factors.py`: momentum, value, quality, low-vol.
3. `models/signal.py`: direction, stop, time limit.
4. `models/meta_label.py`: take or skip, bet size.
5. `models/calibration.py`: Platt or isotonic. Out-of-sample report (P2-6 / X-11).
6. `models/events.py` (X-17): advice only. Deterministic fallback when the LLM is off. Output named `advice_*`.
Gate: no model enters production without a registry record and human sign-off (P2-5 / X-19).

## Step 4 - Conviction, sizing and allocation

Flow lane: sizing and portfolio.
Work, in order:
1. Conviction score (DONE, P2-1). Point-in-time check DONE (X-4).
2. Size tiers (DONE, disabled). Owner approves values only after Step 1 evidence.
3. `portfolio/sizing.py`: fractional Kelly, vol target, hard caps. Final size = min(all caps).
4. `portfolio/allocator.py`: strategy budget (PARTIAL, finish).
5. `portfolio/optimizer.py`, `covariance.py`, `rebalance.py` (DONE, wrapped from `Blue_engine`).
6. `research/capacity.py`: max capital per strategy (PENDING).
Gate: a test proves conviction never raises size past a hard cap. A test proves no module in `risk/`, `execution/`, `portfolio/` imports `core/llm` (X-9).

## Step 5 - Portfolio risk and session guards

Flow lane: portfolio risk, session guards.
Work, in order:
1. `portfolio/portfolio_risk.py`: sector, cluster, beta caps, stress scenarios, CVaR.
2. `risk/controls.py`: halt handler, event blackout (X-8: blocks entries on blackout days, allows exits), overnight gap.
3. `risk/exits.py`, `risk/squareoff.py`: stop, trailing, time exit; MIS cutoff; rollover (X-1 for expiry and lot size).
Gate: tests show that entries are blocked and exits still allowed during blackout and halt.

## Step 6 - Safety gate (the only path to orders)

Flow lane: safety gate.
Work, in order:
1. `risk/pretrade.py`: fat finger, lot size, duplicates, algo ID.
2. `risk/price_band.py`, `risk/liquidity.py`, `risk/loss_breaker.py`.
3. `risk/gate.py`: single entry point for every order intent. Writes an audit row for pass and block.
4. `risk/kill_switch.py` and `monitor/safe_mode.py`: block entries from any stage.
Gate (P1 exit check 1): each hard limit in `config/risk_limits.yaml` has a test that blocks an intent. Gate tests need `openalgo` (owner).

## Step 7 - Execution (Phase 3 only, not enabled)

Flow lane: execution.
Work, in order:
1. `scripts/run_paper.py` end-to-end paper run (P1-5 / X-6). Comes first, before any live code.
2. `execution/queue.py` and `execution/order_fsm.py`: idempotent retries (X-3, no dependencies).
3. Gate-level duplicate block after restart (X-2) and kill-and-restart test (X-7).
4. `execution/openalgo_client.py`: the only file that may call `.placeorder(`. Refuses in paper mode.
5. `execution/router.py`, `execution/protection.py` (P3-3). Stop placed at fill.
Gate: paper run with no orders sent to OpenAlgo. `scripts/run_live.py` is NOT created until Phase 3 exit checks pass.

## Step 8 - Post-trade (what really happened)

Flow lane: post-trade.
Work, in order:
1. `monitor/ledger.py`: intent ID trail, reconciliation (DONE for restart).
2. `monitor/tca.py`: slippage vs arrival price. Feeds back into the cost model (Step 1, item 1).
3. `monitor/live_monitor.py`, `monitor/strategy_health.py`: drawdown, rolling Sharpe, CUSUM auto-disable.
4. `monitor/parity.py` (P3-7): live paper fills vs replayed backtest, within an owner tolerance.
Gate: parity result recorded before any capital decision.

## Step 9 - Improvement loop (changes need a human)

Flow lane: improvement loop.
Work, in order:
1. `feedback/reopt.py`: re-optimization request on fresh bars only.
2. `feedback/champion_challenger.py`: shadow-test new versions.
3. Model registry and human sign-off record (X-19).
4. Every change updates the flow doc, SVG, and AGENTS in the same commit.
Gate: no new version goes live without a sign-off record.

## Step 10 - Support, dashboard, alerts, audit

Flow lane: support, dashboard, alerts.
Work, in order:
1. `audit/log.py` (immutable, append-only), `audit/override_log.py` (DONE).
2. `monitor/throttle.py`, `monitor/session.py`, `monitor/drift.py`.
3. `notify/alerts.py` at INFO, WARNING, ERROR, CRITICAL (P3-5).
4. `reporting/dashboard.py`: P&L shown by default (decision 0007). EV, MAE/MFE, profit factor visible. Rule-compliance panel is NOT NEEDED (owner).
5. `reporting/accounting.py`: password-gated view (DONE).
Gate: critical error shows on the dashboard and triggers safe mode. Restart never resumes live trading without all checks passed.

## Step 11 - Live entry (only after owner sign-off)

Preconditions, all required: Steps 0 to 10 gates passed; P3-8 regulatory checklist verified from official SEBI sources; P3-6 kill switch and safe mode drills done with no orders sent; capital cap set in config.
Start at a small fixed cap. Any scale-up needs owner sign-off.

---

## Per-change loop (every commit, from AGENTS.md, shortened)

1. Find the flow stage and plan ID. If none fits, stop and ask.
2. Read the existing `Blue_engine/` target. Wrap it, do not copy it.
3. Write the code and unit tests (pass and block paths).
4. Run `pytest tests/unit Blue_engine/tests`. Record counts.
5. Check for duplicate code, secrets, and performance.
6. Update docs in the same commit: plan item status, flow doc, SVG if a stage moved, `docs/STATUS_<date>.md`.
7. Commit and push to `master` only after the steps above pass.
8. Append the work-log entry (`Temp/Daily work log/`, git-ignored).

## Next priorities without owner input

Start in this order: X-5, X-8, X-9, X-15 (from `docs/IMPLEMENTATION_PLAN.md`). X-3 and X-4 are done.
Owner items first when available: P0-2, P0-3, P0-4.
