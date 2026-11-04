# Implementation plan: quant system flow v5

Status: updated 2026-11-05. Phases 1-3 have code and unit tests. Phase 3 is NOT enabled.
No live trading path exists. Nothing reaches OpenAlgo from Phase 1 or 2.
Source of truth: `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg`.
Layout: `AGENTS.md` (project layout) and `src/quant/`.
Current state and open decisions: `docs/STATUS_2026-11-05.md`.

Rules for every phase:
- A stage is live only when its code, unit tests and exit checks pass.
- Nothing in Phase 1 or 2 may reach OpenAlgo.
- Only `execution/openalgo_client.py` may call the order API.
- Item status: DONE (code + tests), PARTIAL (code exists, not verified or incomplete), PENDING (not started).

---

## Phase 1: Safe foundation (research and paper only)

Goal: run the pipeline end to end on paper, with no capital.

Status: PARTIAL. Code and unit tests exist. Entry script `scripts/run_paper.py` exists.
Paper run and safety tests not yet confirmed (need `openalgo`).

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Research validation | Historical bars, replay simulator | `research/backtest.py` | PARTIAL |
| Research validation | Data QA gate | `data/qa.py` | PARTIAL |
| Research validation | Indian cost model | `research/costs.py` | PARTIAL |
| Research validation | Walk-forward + purged CV | `research/walk_forward.py` | PARTIAL |
| Research validation | Trial logger, DSR/PBO, bootstrap | `research/trial_log.py`, `research/validation.py` | PARTIAL |
| Research validation | Experiment lineage | `research/lineage.py` | PARTIAL |
| Data | Market data live, feature store | `data/ingest.py`, `data/feature_store.py` | PARTIAL (no live feed) |
| Data | Universe and actions | `data/universe.py`, `data/corp_actions.py` | PARTIAL (not checked in depth) |
| Safety gate | Trade intent, risk gate | `risk/gate.py` | PARTIAL |
| Safety gate | Pre-trade checks | `risk/pretrade.py` | PARTIAL |
| Safety gate | Price band + ASM/GSM | `risk/price_band.py` | PARTIAL |
| Safety gate | Liquidity cap | `risk/liquidity.py` | PARTIAL |
| Safety gate | Loss budget breaker | `risk/loss_breaker.py` | PARTIAL |
| Support | Kill switch | `risk/kill_switch.py` | PARTIAL |
| Support | Safe mode controller | `monitor/safe_mode.py` | PENDING (verify) |
| Audit | Audit log, override log | `audit/log.py`, `audit/override_log.py` | PENDING (verify) |
| Execution | Approved order queue, order state machine | `execution/queue.py`, `execution/order_fsm.py` | PENDING (verify) |
| Post-trade | Ledger and reconciliation | `monitor/ledger.py` | PENDING (verify) |
| Paper | Paper trading stage | `scripts/run_paper.py` | PARTIAL |
| Data | Trading calendar loader | `core/calendar.py`, `config/calendar/holidays.csv` | PENDING (csv is empty) |

Reuse (wrap, do not copy):
- `Blue_engine/core/risk_gate.py`, `Blue_engine/core/kill_switch_store.py`
- `Blue_engine/core/paper_broker.py`, `Blue_engine/core/paper_shift.py`
- `Blue_engine/core/ledger.py`, `Blue_engine/core/ledger_store.py`
- `Blue_engine/backtest/costs.py`, `Blue_engine/backtest/engine.py`, `Blue_engine/backtest/loops.py`
- `Blue_engine/ml/evaluation/walk_forward.py`, `Blue_engine/ml/audit/leakage.py`

Exit checks:
1. Each hard limit in `config/risk_limits.yaml` has a unit test that blocks an intent. Limits hold starter values; owner must approve.
2. Every intent writes an audit record and a ledger row with an intent ID.
3. Backtest costs include STT, brokerage, exchange charges, GST, stamp duty and impact.
4. The trial log counts every trial. DSR and PBO are computed. A point-in-time test shows no look-ahead.
5. Kill the process mid-run, restart, and confirm no duplicate paper orders.

---

## Phase 2: Signal, conviction, sizing and portfolio (still paper only)

Goal: produce target positions that respect caps. Models enter only after a human signs off.
Depends on: all Phase 1 exit checks passing.

Status: PARTIAL. Sizing, covariance, HRP, rebalance and portfolio-risk wraps are in.
Models, signal, conviction and registry are not built. No strategy is approved.
`scripts/run_backtest.py` exists. Validated on synthetic bars only.

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Market inputs | India flow data, bulk/block deals, relative strength, microstructure, filings, global cues | `data/market_inputs.py` | PENDING |
| Models | Regime detector | `models/regime.py` | PENDING |
| Models | Factor and alpha models | `models/factors.py` | PENDING |
| Signal | Event scoring (advice only) | `models/events.py` | PENDING |
| Signal | Signal (direction, stop, time limit) | `models/signal.py` | PENDING |
| Signal | Meta-labeler | `models/meta_label.py` | PENDING |
| Signal | Calibrated conviction (Platt fitter) | `models/calibration.py` | PARTIAL (fitter only) |
| Research | Capacity estimator | `research/capacity.py` | PENDING |
| Sizing | Fractional Kelly | `portfolio/sizing.py` | PARTIAL (wraps Blue) |
| Sizing | Capital allocator | `portfolio/allocator.py` | PARTIAL |
| Sizing | HRP optimizer, covariance, shrinkage | `portfolio/optimizer.py`, `portfolio/covariance.py` | DONE (wraps Blue, tested) |
| Sizing | Drift check, rebalancer | `portfolio/rebalance.py` | DONE (wraps Blue, tested) |
| Portfolio risk | Sector, beta, stress, CVaR | `portfolio/portfolio_risk.py` | PARTIAL |
| Improvement loop | Champion/challenger, model registry, human sign-off | `feedback/champion_challenger.py` | PENDING |
| Improvement loop | Re-opt request | `feedback/reopt.py` | PENDING |
| Monitor | Data drift, live vs backtest parity | `monitor/drift.py`, `monitor/parity.py` | PENDING |

Reuse (wrap, do not copy):
- `Blue_engine/economics/` (regime, providers, event calendar, LLM scorer)
- `Blue_engine/ml/features/point_in_time.py`, `Blue_engine/ml/evaluation/splits.py`
- `Blue_engine/ml/registry/`
- `Blue_engine/core/stage_gate.py` (check fit before use)
- `Blue_engine/core/llm/tokens.py` (frontline and advice paths only)

Exit checks:
1. Out-of-sample results after costs, with DSR and PBO reported. No strategy enters without them.
2. A conviction calibration report exists. Conviction never overrides a hard cap.
3. A test proves no module in `risk/`, `execution/` or `portfolio/` imports `core/llm`.
4. Each model version has a human sign-off record in the registry.
5. Event scoring output is logged as advice only. It never sets size.

---

## Phase 3: Live execution, session guards and operations

Goal: go live in small steps, with every guard tested.
Depends on: Phase 2 exit checks, plus a paper period with parity results.

Status: PENDING for the live path. Risk control modules exist (controls, exits, squareoff)
but are not enabled. `scripts/run_live.py` is deliberately NOT created.

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Session guards | Market-halt handler, event blackout, overnight gap | `risk/controls.py` | PARTIAL |
| Session guards | Exit manager, square-off and rollover | `risk/exits.py`, `risk/squareoff.py` | PARTIAL |
| Live risk | Live intraday VaR/CVaR | `portfolio/portfolio_risk.py` | PARTIAL |
| Execution | Algo router | `execution/router.py` | PENDING |
| Execution | Protection (stop at fill) | `execution/protection.py` | PENDING |
| Execution | OpenAlgo adapter | `execution/openalgo_client.py` | PENDING |
| Feeds | Broker live feed | `data/ingest.py` | PENDING (CSV and heartbeat only) |
| Support | Broker session manager | `monitor/session.py` | PENDING |
| Support | Order-rate throttle | `monitor/throttle.py` | PENDING |
| Post-trade | Live monitor | `monitor/live_monitor.py` | PENDING |
| Post-trade | Strategy health | `monitor/strategy_health.py` | PENDING |
| Post-trade | TCA to cost model | `monitor/tca.py` | PENDING |
| Reporting | Dashboard (see Priority B) | `reporting/dashboard.py` | PARTIAL |
| Reporting | Accounting view (authorised) | `reporting/accounting.py` | PENDING |
| Notify | Alerts | `notify/alerts.py` | PENDING |

Reuse (wrap, do not copy):
- `Blue_engine/core/client.py`, `Blue_engine/core/broker_auth.py`, `Blue_engine/core/order_updates.py`
- `Blue_engine/core/quote_health.py`, `Blue_engine/core/throttle.py`
- `Blue_engine/core/exit_engine.py`, `Blue_engine/core/protective_orders.py`, `Blue_engine/core/auction_windows.py`
- `Blue_engine/core/live_monitor.py`, `Blue_engine/core/lot.py`
- `Blue_engine/dashboard/app.py`. Check `pov_state_machine.py` first: it is dashboard state, not an order router.

Exit checks (all required before real capital):
1. Live paper fills match the replayed backtest within a tolerance the owner sets.
2. Regulatory items (SEBI algo registration, order tagging, immutable audit store, NTP clock sync) checked against current SEBI rules. NOT verified yet.
3. Capital starts at a fixed, small cap in config. Any scale-up needs owner sign-off.
4. Kill switch and safe mode tested in a live session with no orders sent.
5. Dashboard P&L visibility matches the owner decision (see Priority B).

---

## Implementation list (numbered, in order)

Order rule: finish a block before starting the next. Each item ends with tests, a doc update, and a commit.

### P0 - Decisions and test baseline (do first)
- **P0-1** Owner decides P&L visibility default (`STATUS_2026-11-05.md`, section 3). PENDING (owner).
- **P0-2** Install `openalgo` on owner machine; run `pytest tests/unit Blue_engine/tests`; record pass/fail counts in the status note. PENDING (owner machine).
- **P0-3** Owner approves or changes starter risk limits in `config/risk_limits.yaml`. PENDING (owner).
- **P0-4** Run `scripts/research/fetch_nse_holidays.py` on owner machine; fill `config/calendar/holidays.csv`; confirm BSE dates. PENDING (owner machine).

### P1 - Phase 1 finish (paper)
- **P1-1** Build `core/calendar.py` loader on top of `holidays.csv`. Tests: weekend, holiday, expiry-day cases. PENDING.
- **P1-2** Verify and complete `monitor/safe_mode.py`, `audit/log.py`, `audit/override_log.py`. Each needs a block-path test. PENDING.
- **P1-3** Verify and complete `execution/queue.py`, `execution/order_fsm.py` (idempotent retries). PENDING.
- **P1-4** Verify `monitor/ledger.py` reconciliation; add duplicate-order test on restart. PENDING.
- **P1-5** Run paper pipeline end to end with `scripts/run_paper.py`; record result in status note. PENDING.
- **P1-6** Real-data OCL backtest with costs, walk-forward, DSR and PBO. PENDING.
- **P1-7** Confirm `data/universe.py` (survivorship-free) and `data/corp_actions.py` in depth. PENDING.

### P2 - Phase 2 finish (paper)
- **P2-1** Conviction engine `portfolio/conviction.py` (six conditions, weighted geometric mean, breakdown output). PENDING. See Priority A.
- **P2-2** Size tiers `portfolio/tiers.py` and `config/conviction_tiers.yaml`, disabled by default. PENDING.
- **P2-3** Wire tiers into sizing with min-of-caps; write an audit record for each size decision. PENDING.
- **P2-4** Signal, meta-labeler, regime detector, factor models (`models/*.py`). PENDING.
- **P2-5** Model registry with human sign-off record. PENDING.
- **P2-6** Conviction calibration report (out-of-sample only). PENDING.
- **P2-7** Test that `risk/`, `execution/`, `portfolio/` never import `core/llm`. PENDING.
- **P2-8** Market-inputs module `data/market_inputs.py` (flow, bulk/block deals, filings). PENDING.

### PB - Priority B: P&L-blind dashboard
- **PB-1** Hide P&L by default in `Blue_engine/dashboard/render.py`, `dashboard/algo_pro/index.html`, `reporting/dashboard.py`, if P0-1 says hidden. Tests: default render has no money figures. PENDING (needs P0-1).
- **PB-2** Toggle to reveal money figures; toggle state not stored by default. PENDING.
- **PB-3** Execution panels: EV, profit factor, risk-to-reward, MAE, MFE, win rate, rule compliance, deviation alerts. Reuse `Blue_engine/backtest/metrics.py` after check. PENDING.
- **PB-4** Accounting view behind an authorised action (method set by owner, status note section 3). PENDING.
- **PB-5** Hand-checked tests for EV, profit factor, MAE/MFE on a fixed trade list. PENDING.

### P3 - Phase 3 (live path, NOT enabled)
- **P3-1** Broker live feed in `data/ingest.py` (replace CSV-only path). PENDING.
- **P3-2** `execution/openalgo_client.py` adapter (only file that may contain `.placeorder(`). PENDING.
- **P3-3** `execution/router.py` and `execution/protection.py`. PENDING.
- **P3-4** Session manager, throttle, live monitor, strategy health, TCA. PENDING.
- **P3-5** Alerts (`notify/alerts.py`) at INFO / WARNING / ERROR / CRITICAL with dashboard feed. PENDING.
- **P3-6** Kill switch and safe mode drills, no orders sent. PENDING.
- **P3-7** Parity test: live paper fills vs replayed backtest. PENDING.
- **P3-8** Regulatory checklist verified against current SEBI rules, from official sources. PENDING.

### Docs and governance
- **D-1** Update the `AGENTS.md` status line (Phase 0 is done). Full rewrite; owner approval needed. PENDING.
- **D-2** Review `docs/quant_system_flow_v5.md` and `docs/ALGO_FLOW.md` against this plan. PENDING.
- **D-3** Daily SOP entry at `docs/My_trading_system_SOP/SOP.md`. PENDING (file not reviewed).
- **D-4** Work-log entry after each commit (`Temp/Daily work log/`, git-ignored, not pushed). Ongoing.

---

## Open gaps and assumptions

- `config/calendar/holidays.csv` is effectively empty. No trading day is verified until P0-4 is done.
- Risk limit values are starter paper values, not owner-approved.
- Paper period length, parity tolerance and starting capital are not set.
- Blue_engine reuse targets are named by path. Read and confirm each before wrapping.
- The test count is from the 11-04 run. It has not been re-run.
