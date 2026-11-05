# Implementation plan: quant system flow v5

Status: updated 2026-11-05 (after merging `feat/conviction-score-v2` into `master`).
Phases 1-3 have code and unit tests. Phase 3 live path is NOT enabled.
No live trading path exists. Nothing reaches OpenAlgo from Phase 1 or 2.
Source of truth: `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg`.
Layout: `AGENTS.md` (project layout) and `src/quant/`.
Current state and open decisions: `docs/STATUS_2026-11-05.md`. P&L decision: `docs/decisions/0007-pnl-shown-by-default.md`.

Rules for every phase:
- A stage is live only when its code, unit tests and exit checks pass.
- Nothing in Phase 1 or 2 may reach OpenAlgo.
- Only `execution/openalgo_client.py` may call the order API.
- Item status: DONE (code + tests pass here), PARTIAL (code exists, some checks missing), PENDING (not started or needs owner/data/network).

Test state (2026-11-05): full unit run in this sandbox gives 154 passed, 34 skipped, 70 failed. All 70 failures are `ModuleNotFoundError: openalgo` (PyPI blocked here). The new and changed test files pass: 91 passed, 7 skipped (terminal-dashboard tests need `rich` and `Blue_engine`).

---

## Phase 1: Safe foundation (research and paper only)

Goal: run the pipeline end to end on paper, with no capital.

Status: PARTIAL. Entry script `scripts/run_paper.py` exists. Paper run not yet confirmed (needs `openalgo`).

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
| Safety gate | Trade intent, risk gate | `risk/gate.py` | PARTIAL (gate tests need `openalgo`) |
| Safety gate | Pre-trade checks | `risk/pretrade.py` | PARTIAL (needs `openalgo`) |
| Safety gate | Price band + ASM/GSM | `risk/price_band.py` | PARTIAL (needs `openalgo`) |
| Safety gate | Liquidity cap | `risk/liquidity.py` | PARTIAL (needs `openalgo`) |
| Safety gate | Loss budget breaker | `risk/loss_breaker.py` | PARTIAL (needs `openalgo`) |
| Support | Kill switch | `risk/kill_switch.py` | PARTIAL |
| Support | Safe mode controller | `monitor/safe_mode.py` | PARTIAL: reason rule tested (`test_phase1_gaps.py`) |
| Audit | Audit log | `audit/log.py` | PARTIAL: tamper test passes (`test_phase1_risk_exec.py`) |
| Audit | Override log | `audit/override_log.py` | DONE: actor and reason rule tested (`test_phase1_gaps.py`) |
| Execution | Approved order queue | `execution/queue.py` | PARTIAL: refuses unapproved intents (tested) |
| Execution | Order state machine | `execution/order_fsm.py` | PARTIAL: rejects bad transitions (tested) |
| Post-trade | Ledger and reconciliation | `monitor/ledger.py` | DONE: restart keeps intent IDs and positions (`test_phase1_gaps.py`) |
| Paper | Paper trading stage | `scripts/run_paper.py` | PARTIAL (end-to-end run pending) |
| Data | Trading calendar loader | `core/calendar.py`, `config/calendar/holidays.csv` | DONE: 2026 NSE holidays filled; tests pass. Expiry-day cases NOT covered yet. |

Reuse (wrap, do not copy):
- `Blue_engine/core/risk_gate.py`, `Blue_engine/core/kill_switch_store.py`
- `Blue_engine/core/paper_broker.py`, `Blue_engine/core/paper_shift.py`
- `Blue_engine/core/ledger.py`, `Blue_engine/core/ledger_store.py`
- `Blue_engine/backtest/costs.py`, `Blue_engine/backtest/engine.py`, `Blue_engine/backtest/loops.py`
- `Blue_engine/ml/evaluation/walk_forward.py`, `Blue_engine/ml/audit/leakage.py`

Exit checks:
1. Each hard limit in `config/risk_limits.yaml` has a unit test that blocks an intent. Gate tests need `openalgo`. Limits hold starter values; owner must approve.
2. Every intent writes an audit record and a ledger row with an intent ID. Partly tested.
3. Backtest costs include STT, brokerage, exchange charges, GST, stamp duty and impact. Cost model wrapped; check the cost tests once `openalgo` is installed.
4. The trial log counts every trial. DSR and PBO computed. Point-in-time test for look-ahead: PENDING.
5. Kill the process mid-run, restart, and confirm no duplicate paper orders. Ledger part DONE; full process test PENDING.

---

## Phase 2: Signal, conviction, sizing and portfolio (still paper only)

Goal: produce target positions that respect caps. Models enter only after a human signs off.
Depends on: all Phase 1 exit checks passing.

Status: PARTIAL. Conviction score, size tiers, and cap wiring are on `master` (merged from the earlier feature branch). Models, signals and registry are not built. No strategy is approved.

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Market inputs | NSE board-meeting event calendar | `data/market_inputs.py` (`parse_nse_event_calendar`, `event_blackout_dates`) | DONE: parser and blackout window, tests pass (`test_event_calendar.py`) |
| Market inputs | India flow data, bulk/block deals, relative strength, microstructure, global cues | `data/market_inputs.py` | PARTIAL (relative strength and order-book helpers exist; flow and bulk/block PENDING) |
| Models | Regime detector | `models/regime.py` | PENDING |
| Models | Factor and alpha models | `models/factors.py` | PARTIAL (momentum and profit factor wrapped) |
| Signal | Event scoring (advice only) | `models/events.py` | PENDING |
| Signal | Signal (direction, stop, time limit) | `models/signal.py` | PENDING |
| Signal | Meta-labeler | `models/meta_label.py` | PENDING |
| Signal | Conviction score (six conditions) | `models/conviction.py` | DONE: tests pass (`test_conviction.py`) |
| Signal | Calibrated conviction (Platt fitter) | `models/calibration.py` | PARTIAL (fitter only) |
| Research | Capacity estimator | `research/capacity.py` | PENDING |
| Sizing | Fractional Kelly | `portfolio/sizing.py` | DONE (wraps Blue, tested) |
| Sizing | Size tiers and cap wiring | `portfolio/tiers.py`, `config/conviction_tiers.yaml` | DONE: disabled by default; min-of-caps tested; audit record returned |
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
1. Out-of-sample results after costs, with DSR and PBO reported. PENDING (needs real data).
2. A conviction calibration report exists. Conviction never overrides a hard cap. Cap rule tested; report PENDING.
3. A test proves no module in `risk/`, `execution/` or `portfolio/` imports `core/llm`. PENDING.
4. Each model version has a human sign-off record in the registry. PENDING.
5. Event scoring output is logged as advice only. It never sets size. Event scoring PENDING; NSE blackout dates are advice-level only (documented).

---

## Phase 3: Live execution, session guards and operations

Goal: go live in small steps, with every guard tested.
Depends on: Phase 2 exit checks, plus a paper period with parity results.

Status: PENDING for the live path. Risk control modules exist (controls, exits, squareoff) but are not enabled. `scripts/run_live.py` is deliberately NOT created.

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Session guards | Market-halt handler, event blackout, overnight gap | `risk/controls.py` | PARTIAL (blackout helper takes dates from `event_blackout_dates`) |
| Session guards | Exit manager, square-off and rollover | `risk/exits.py`, `risk/squareoff.py` | PARTIAL |
| Live risk | Live intraday VaR/CVaR | `portfolio/portfolio_risk.py` | PARTIAL |
| Execution | Algo router | `execution/router.py` | PENDING |
| Execution | Protection (stop at fill) | `execution/protection.py` | PENDING |
| Execution | OpenAlgo adapter | `execution/openalgo_client.py` | PARTIAL (refuses in paper and without approval; tested) |
| Feeds | Broker live feed | `data/ingest.py` | PENDING (CSV and heartbeat only) |
| Support | Broker session manager | `monitor/session.py` | PENDING |
| Support | Order-rate throttle | `monitor/throttle.py` | PENDING |
| Post-trade | Live monitor | `monitor/live_monitor.py` | PENDING |
| Post-trade | Strategy health | `monitor/strategy_health.py` | PENDING |
| Post-trade | TCA to cost model | `monitor/tca.py` | PENDING |
| Reporting | Dashboard (P&L shown by default) | `reporting/dashboard.py`, `Blue_engine/dashboard/render.py`, `dashboard/algo_pro/index.html` | DONE for P&L visibility (decision 0007). Other panels PARTIAL. |
| Reporting | Accounting view (password-gated) | `reporting/accounting.py`, `scripts/set_accounting_password.py` | DONE: password lockout tested (`test_accounting_password.py`) |
| Notify | Alerts | `notify/alerts.py` | PENDING |

Reuse (wrap, do not copy):
- `Blue_engine/core/client.py`, `Blue_engine/core/broker_auth.py`, `Blue_engine/core/order_updates.py`
- `Blue_engine/core/quote_health.py`, `Blue_engine/core/throttle.py`
- `Blue_engine/core/exit_engine.py`, `Blue_engine/core/protective_orders.py`, `Blue_engine/core/auction_windows.py`
- `Blue_engine/core/live_monitor.py`, `Blue_engine/core/lot.py`
- `Blue_engine/dashboard/app.py`. Check `pov_state_machine.py` first: it is dashboard state, not an order router.

Exit checks (all required before real capital):
1. Live paper fills match the replayed backtest within a tolerance the owner sets. PENDING.
2. Regulatory items (SEBI algo registration, order tagging, immutable audit store, NTP clock sync) checked against current SEBI rules. NOT verified yet.
3. Capital starts at a fixed, small cap in config. Any scale-up needs owner sign-off.
4. Kill switch and safe mode tested in a live session with no orders sent. PENDING.
5. Dashboard P&L shown by default (decision 0007). EV, MAE/MFE and rule compliance visible. P&L part DONE; rule compliance panel PENDING.

---

## Implementation list (numbered, in order)

Order rule: finish a block before starting the next. Each item ends with tests, a doc update, and a commit.

### P0 - Decisions and test baseline
- **P0-1** P&L visibility. DONE 2026-11-05: owner chose P&L shown by default (decision 0007).
- **P0-2** Install `openalgo` on owner machine; run `pytest tests/unit Blue_engine/tests`; record pass/fail counts. PENDING (owner machine; PyPI blocked here).
- **P0-3** Owner approves or changes starter risk limits in `config/risk_limits.yaml`. PENDING (owner).
- **P0-4** Holiday list: 2026 NSE holidays are on `master`. Owner to verify once against the NSE circular. PENDING (owner). BSE dates still to confirm.

### P1 - Phase 1 finish (paper)
- **P1-1** Calendar loader. DONE (2026 holidays; tests). Still to add: expiry-day cases. PENDING (small).
- **P1-2** Safe mode, audit log, override log. PARTIAL: override log and safe-mode reason rule tested. Block-path tests for the full gate need `openalgo`.
- **P1-3** Order queue and order state machine. PARTIAL: tests pass. Idempotent retry test DONE (X-3). Duplicate-order block after restart still open (X-2).
- **P1-4** Ledger reconciliation and restart. PARTIAL: restart keeps intent IDs and positions (DONE). Duplicate order block at gate level needs `openalgo`.
- **P1-5** Paper pipeline end to end with `scripts/run_paper.py`. PENDING (needs `openalgo`).
- **P1-6** Real-data OCL backtest with costs, walk-forward, DSR and PBO. PENDING (NSE bars not reachable from the sandbox).
- **P1-7** Survivorship-free universe and corporate actions in depth. PENDING.

### P2 - Phase 2 finish (paper)
- **P2-1** Conviction score (`models/conviction.py`). DONE: tests pass.
- **P2-2** Size tiers (`portfolio/tiers.py`, `config/conviction_tiers.yaml`), disabled by default. DONE: tests pass.
- **P2-3** Wire tiers into sizing with min-of-caps and an audit record. DONE: tests pass (cap binding and de-risking tested).
- **P2-4** Signal, meta-labeler, regime detector, factor models. PENDING.
- **P2-5** Model registry with human sign-off record. PENDING.
- **P2-6** Conviction calibration report (out-of-sample only). PENDING (needs real data).
- **P2-7** Test that `risk/`, `execution/`, `portfolio/` never import `core/llm`. PENDING.
- **P2-8** Market inputs for flow, bulk/block deals, filings. PARTIAL: NSE board-meeting event parser and blackout dates DONE. Flow and bulk/block PENDING.

### PB - Priority B: dashboard P&L and panels
- **PB-1** P&L shown by default on terminal and web dashboards. DONE (reversed from hidden; decision 0007).
- **PB-2** Toggle hides money figures on screen. DONE: tests pass.
- **PB-3** Execution panels: EV, profit factor, risk-to-reward, MAE, MFE, win rate, rule compliance, deviation alerts. PARTIAL (EV, profit factor, MAE/MFE in quant dashboard; rule-compliance panel and deviation alerts NOT NEEDED, owner 2026-11-05).
- **PB-4** Accounting view behind an authorised action. DONE (password lockout; tested). Owner to confirm the method.
- **PB-5** Hand-checked tests for EV, profit factor, MAE/MFE. PARTIAL (quant dashboard tests exist; check values once `openalgo` is installed).

### P3 - Phase 3 (live path, NOT enabled)
- **P3-1** Broker live feed in `data/ingest.py`. PENDING.
- **P3-2** `execution/openalgo_client.py` adapter. PARTIAL (refuses in paper; real call path PENDING).
- **P3-3** `execution/router.py` and `execution/protection.py`. PENDING.
- **P3-4** Session manager, throttle, live monitor, strategy health, TCA. PENDING.
- **P3-5** Alerts (`notify/alerts.py`) at INFO / WARNING / ERROR / CRITICAL with dashboard feed. PENDING.
- **P3-6** Kill switch and safe mode drills, no orders sent. PENDING.
- **P3-7** Parity test: live paper fills vs replayed backtest. PENDING.
- **P3-8** Regulatory checklist verified against current SEBI rules, from official sources. PENDING.

### Docs and governance
- **D-1** Update `AGENTS.md` status line and rule 8 (P&L now shown; Phase 0 done). Full rewrite; owner approval needed. PENDING.
- **D-2** Review flow doc and `ALGO_FLOW.md`. DONE 2026-11-05: flow doc P&L and event-blackout lines fixed; SVG label fixed; `ALGO_FLOW.md` is only a pointer (no change needed).
- **D-3** Daily SOP entry at `docs/My_trading_system_SOP/SOP.md`. PENDING (file not reviewed).
- **D-4** Work-log entry after each commit (`Temp/Daily work log/`, git-ignored, not pushed). Ongoing.

---

## Additional priority tasks (added 2026-11-05)

Order: work top to bottom inside each tier. Tier A comes before any capital decision.
Each task ends with tests, a doc update, and a commit. "Needs" says what blocks it.

### Tier A - safety and evidence (before any capital)
- **X-1** Expiry-day and lot-size handling in the calendar loader (`core/calendar.py`, `config/calendar/`). Needs: owner-verified expiry and lot-size list.
- **X-2** Gate-level duplicate-order block after restart (P1-4 remainder). Needs: `openalgo` installed (owner).
- **X-3** Idempotent retry test in the order state machine (P1-3 remainder). DONE 2026-11-05: repeated reports are no-ops, conflicting or stale fills are refused, and a refused move changes nothing (`tests/unit/test_order_fsm_retry.py`). Fixed a bug where a refused FILLED report still changed the fill quantity.
- **X-4** Point-in-time test: conviction inputs and feature store read only past bars (P2-1 exit check, A4). DONE 2026-11-05: late writes, out-of-order writes, inclusive release time, no forward API, and conviction-from-store unchanged by future data (`tests/unit/test_point_in_time_conviction.py`). Store was already correct; tests catch a leaky store (checked with a mutant).
- **X-5** Cost-model tests for STT, brokerage, exchange charges, GST, stamp duty and impact, using the owner's cost rules (commit `4dab074`). DONE 2026-11-05: 23 hand-calculated rupee tests run without OpenAlgo (`tests/unit/test_cost_owner_rules.py`); side and product strings are now validated (a lowercase "sell" used to be charged as a buy); two tautological asserts removed from `test_phase0_wrappers.py` and `test_phase1_research.py`. Still open: owner to confirm the rates against NSE, BSE and SEBI schedules (`costs.py` says the defaults are placeholders).
- **X-6** End-to-end paper run with `scripts/run_paper.py` (P1-5). Needs: `openalgo` (owner).
- **X-7** Kill-and-restart test mid-run: no duplicate paper orders (P1 exit check 5). Needs: none for ledger-level; full process needs `openalgo`.
- **X-8** Event blackout wired into entry checks in `risk/controls.py`, with a test that blocks entries on blackout days and allows exits. Needs: none.
- **X-9** Layout test: no import of `core/llm` from `risk/`, `execution/`, `portfolio/` (P2-7). Needs: none.

### Tier B - research evidence (needs real data)
- **X-10** Real-data OCL backtest with costs, walk-forward, DSR and PBO (P1-6). Needs: NSE bar data from owner; sandbox cannot reach NSE.
- **X-11** Out-of-sample conviction calibration report (P2-6). Needs: X-10 data.
- **X-12** Trial log entry for every optimisation run (AGENTS rule: sampler, trial count, bounds, folds, cost model version). Needs: none for the wiring; real runs need X-10.
- **X-13** Survivorship-free universe and corporate-action review (P1-7). Needs: owner data source for delisted symbols.

### Tier C - dashboard
- **X-14** NOT NEEDED (owner, 2026-11-05). Was: rule-compliance panel and strategy deviation alerts (PB-3 remainder).
- **X-15** Hand-checked tests for EV, profit factor, MAE/MFE on a fixed trade list (PB-5). Needs: none.
- **X-16** NOT NEEDED (owner, 2026-11-05). Was: upcoming NSE board meetings on the dashboard. The parser and blackout helper stay, because X-8 uses them.

### Tier D - signal build-out (paper only)
- **X-17** Event scoring, advice only, with a deterministic fallback when the LLM is off (P2 events). Needs: none for the fallback; LLM provider is optional.
- **X-18** Regime detector and factor models (P2-4). Needs: X-10 data to validate.
- **X-19** Model registry with human sign-off record (P2-5). Needs: owner sign-off process.

### Tier E - governance and docs (owner actions)
- **X-20** Rewrite `AGENTS.md` status line and rule 8 (P&L now shown; Phase 0 done) (D-1). Needs: owner approval.
- **X-21** Review `docs/My_trading_system_SOP/SOP.md` against reality (D-3). Needs: none.
- **X-22** Delete stale remote branches `main` (`76f21af`) and `docs/status-2026-11-05`. Needs: owner (sandbox cannot delete branches).
- **X-23** Verify the 2026 holiday list against the NSE circular (P0-4). Needs: owner.
- **X-24** SEBI algo-trading checklist (registration, order tagging, immutable audit store, clock sync) from official sources (P3-8). Needs: owner research; sandbox can reach SEBI via search only.

### Next to start without the owner
X-8, X-9, X-15 (X-3, X-4 and X-5 done 2026-11-05). These need no data, no install, and no decision.

---

## Open gaps and assumptions

- Expiry days and lot sizes are not handled by the calendar loader.
- Risk limit values are starter paper values, not owner-approved.
- Paper period length, parity tolerance and starting capital are not set.
- Event blackout uses the NSE board-meeting feed. It is a blocking input only, not a signal. The sandbox cannot reach NSE from code; the sample used in tests is a saved fixture.
- Blue_engine reuse targets are named by path. Read and confirm each before wrapping.
- `AGENTS.md` still says P&L is hidden by default. Superseded by decision 0007 until `AGENTS.md` is rewritten.
