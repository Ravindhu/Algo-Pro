# Implementation plan: quant system flow v5

Status: updated 2026-11-06 (owner answers and first fixes; see `docs/STATUS_2026-11-06.md` and `docs/decisions/0015-owner-answers-2026-11-06.md`). Earlier: 2026-11-05 doc sync.
Phases 1-3 have code and unit tests. Phase 3 live path is NOT enabled.
No live trading path exists. Nothing reaches OpenAlgo from Phase 1 or 2.
Source of truth: `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg`.
Layout: `AGENTS.md` (project layout) and `src/quant/`.
Current state and open decisions: `docs/STATUS_2026-11-05.md`. P&L decision: `docs/decisions/0007-pnl-shown-by-default.md`. Dashboard panels and MAE/MFE: `docs/decisions/0008-dashboard-metrics-and-optimizer-excursion-weights.md`.

## Doc map (which file owns what)

- `docs/IMPLEMENTATION_PLAN.md` (this file): phase status and the item list. Item status lives here.
- `docs/PRIORITY_IMPLEMENTATION_PLAN.md`: detail for conviction sizing and dashboard P&L items.
- `docs/IMPLEMENTATION_WORKFLOW.md`: order of work from start to end, in money-flow order.
- `docs/audits/PHASE0_REUSE_AUDIT.md`: decision for each module that wraps or keeps `Blue_engine/` code.
- `docs/STATUS_2026-11-05.md`: latest plain-English state of the repo.
- `docs/decisions/`: owner and design decisions (0001 to 0010; 0009 and 0010 are proposals).
- `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg`: stage design (source of truth).
- `AGENTS.md`: project layout and rules. Still needs a rewrite (D-1).

Rules for every phase:
- A stage is live only when its code, unit tests and exit checks pass.
- Nothing in Phase 1 or 2 may reach OpenAlgo.
- Only `execution/openalgo_client.py` may call the order API.
- Item status: DONE (code + tests pass here), PARTIAL (code exists, some checks missing), PENDING (not started or needs owner/data/network).

Test state (2026-11-05, after the dashboard and optimizer change): `pytest tests/unit Blue_engine/tests` gives 378 passed, 112 skipped, 107 failed, 1 xfailed. The 107 failures are the same set as before the change (checked by stashing the change and re-running). Most are `ModuleNotFoundError: openalgo` (PyPI blocked here). X-28 lists the real Blue logic failures.
The owner-machine baseline (P0-2) is still needed to confirm the full suite.

---

## Phase 1: Safe foundation (research and paper only)

Goal: run the pipeline end to end on paper, with no capital.

Status: PARTIAL. Entry script `scripts/run_paper.py` exists. Paper run not yet confirmed (needs `openalgo`).

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Research validation | Historical bars, replay simulator | `research/backtest.py` | PARTIAL |
| Research validation | Data QA gate | `data/qa.py` | PARTIAL |
| Research validation | Indian cost model | `research/costs.py` | PARTIAL (rule tests DONE, X-5; exchange charge rate still to verify) |
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
| Execution | Order state machine | `execution/order_fsm.py` | PARTIAL: rejects bad transitions; idempotent retry DONE (X-3) |
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
3. Backtest costs include STT, brokerage, exchange charges, GST, stamp duty and impact. Rule tests DONE (X-5, hand-calculated tests, no `openalgo` needed). Exchange charge rate DONE 2026-11-06 (0.00307%, NSE circular FA73061). Impact is a flat 5 bps; square-root impact not used yet (X-30).
4. The trial log counts every trial. Point-in-time (look-ahead) test DONE for conviction inputs and feature store (X-4). DSR and PBO on real data: PENDING (X-10).
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
| Models | Regime detector | `models/regime.py` | PENDING (Blue comparison DONE: decision 0005, KEEP local) |
| Models | Factor and alpha models | `models/factors.py` | PARTIAL (momentum and profit factor wrapped) |
| Signal | Event scoring (advice only) | `models/events/scoring.py` | PARTIAL: keyword fallback DONE (X-17, `score_filing_advice`); optional LLM scorer not wired to a caller yet |
| Signal | Signal (direction, stop, time limit) | `models/signal.py` | PENDING |
| Signal | Meta-labeler | `models/meta_label.py` | PENDING |
| Signal | Conviction score (six conditions) | `models/conviction.py` | DONE: tests pass (`test_conviction.py`) |
| Signal | Calibrated conviction (Platt fitter) | `models/calibration.py` | PARTIAL (fitter only) |
| Research | Capacity estimator | `research/capacity.py` | PENDING |
| Sizing | Fractional Kelly | `portfolio/sizing.py` | DONE (wraps Blue, tested) |
| Sizing | Size tiers and cap wiring | `portfolio/tiers.py`, `config/conviction_tiers.yaml` | DONE: disabled by default; min-of-caps tested; audit record returned |
| Sizing | Capital allocator | `portfolio/allocator.py` | PARTIAL |
| Sizing | HRP optimizer, covariance, shrinkage | `portfolio/optimizer.py`, `portfolio/covariance.py` | DONE for shrinkage (wraps Blue, decision 0003). HRP KEEP local; Blue HRP disagrees (X-25). |
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
1. Out-of-sample results after costs, with DSR and PBO reported. PENDING (needs real data, X-10).
2. A conviction calibration report exists. Conviction never overrides a hard cap. Cap rule tested; report PENDING (X-11).
3. A test proves no module in `risk/`, `execution/` or `portfolio/` imports `core/llm`. DONE (X-9, AST scan of 26 files). Remaining gap: the package-level import chain (X-9b).
4. Each model version has a human sign-off record in the registry. PENDING (X-19).
5. Event scoring output is logged as advice only. It never sets size. Event scoring PENDING (X-17); NSE blackout dates are advice-level only (documented).

---

## Phase 3: Live execution, session guards and operations

Goal: go live in small steps, with every guard tested.
Depends on: Phase 2 exit checks, plus a paper period with parity results.

Status: PENDING for the live path. Risk control modules exist (controls, exits, squareoff) but are not enabled. `scripts/run_live.py` is deliberately NOT created.

| Flow lane | Blocks | Module | Status |
|---|---|---|---|
| Session guards | Market-halt handler, event blackout, overnight gap | `risk/controls.py` | PARTIAL (entry blackout block DONE, X-8; no runner feeds blackout dates yet) |
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
| Reporting | Dashboard (P&L shown by default) | `reporting/dashboard.py`, `reporting/dashboard_data.py`, `dashboard/app.py` (Blue terminal console removed 2026-11-05), `dashboard/index.html` | DONE for P&L visibility (decision 0007). EV, MAE/MFE and rule compliance removed (decision 0008). |
| Reporting | Accounting view (password-gated) | `reporting/accounting.py`, `scripts/set_accounting_password.py` | DONE: password lockout tested (`test_accounting_password.py`) |
| Notify | Alerts | `notify/alerts.py` | PENDING |

Reuse (wrap, do not copy):
- `Blue_engine/core/client.py`, `Blue_engine/core/broker_auth.py`, `Blue_engine/core/order_updates.py`
- `Blue_engine/core/quote_health.py`, `Blue_engine/core/throttle.py`
- `Blue_engine/core/exit_engine.py`, `Blue_engine/core/protective_orders.py`, `Blue_engine/core/auction_windows.py`
- `Blue_engine/core/live_monitor.py`, `Blue_engine/core/lot.py`
- ~~`Blue_engine/dashboard/app.py`~~ removed 2026-11-05 (see docs/DASHBOARD_REMOVAL_PLAN.md). Dashboard is `dashboard/app.py` (FastAPI).

Exit checks (all required before real capital):
1. Live paper fills match the replayed backtest within a tolerance the owner sets. PENDING.
2. Regulatory items (SEBI algo registration, order tagging, immutable audit store, NTP clock sync) checked against current SEBI rules. NOT verified yet. Scope is an OPEN OWNER QUESTION (see "Open owner decisions").
3. Capital starts at a fixed, small cap in config. Any scale-up needs owner sign-off.
4. Kill switch and safe mode tested in a live session with no orders sent. PENDING.
5. Dashboard P&L shown by default (decision 0007). EV, MAE/MFE and rule compliance removed; profit factor and win rate visible (decision 0008).

---

## Implementation list (numbered, in order)

Order rule: finish a block before starting the next. Each item ends with tests, a doc update, and a commit.

### P0 - Decisions and test baseline
- **P0-1** P&L visibility. DONE 2026-11-05: owner chose P&L shown by default (decision 0007).
- **P0-2** Install `openalgo` on owner machine; run `pytest tests/unit Blue_engine/tests`; record pass/fail counts. PENDING (owner machine). 2026-11-06: sandbox install blocked (PyPI 403); sandbox counts: 70 failed, 311 passed, all failures `httpx` missing. Also needed to classify the Blue failures in X-28.
- **P0-3** Owner approves or changes starter risk limits in `config/risk_limits.yaml`. PAPER: DONE 2026-11-06 (owner: set minimum values; approved for paper only). LIVE values: still PENDING (owner).
- **P0-4** Holiday list: 2026 NSE holidays on `master`; owner says updated. 2026-11-06 check vs NSE circular CD71962: 15 match; 4 circular dates not in CSV (2026-02-19, 2026-03-19, 2026-04-01, 2026-08-26); Muhurat 2026-11-08 not recorded. PENDING owner decision. BSE dates still to confirm.

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
- **P2-7** Test that `risk/`, `execution/`, `portfolio/` never import `core/llm`. DONE (X-9). Package-level import chain still open (X-9b).
- **P2-8** Market inputs for flow, bulk/block deals, filings. PARTIAL: NSE board-meeting event parser and blackout dates DONE. Flow and bulk/block PENDING.

### PB - Priority B: dashboard P&L and panels
- **PB-1** P&L shown by default on terminal and web dashboards. DONE (reversed from hidden; decision 0007).
- **PB-2** Toggle hides money figures on screen. DONE: tests pass.
- **PB-3** Dashboard panels. Owner rule (decision 0008, 2026-11-05): show P&L, win rate, profit factor, risk utilisation. Remove EV, MAE/MFE and rule compliance. DONE for the quant dashboard payload and the web dashboard. Risk-to-reward and execution quality are NOT confirmed in code; check before marking them. Deviation alerts: NOT NEEDED (owner).
- **PB-4** Accounting view behind an authorised action. DONE (password lockout; tested). Owner to confirm the method.
- **PB-5** Hand-checked tests for profit factor, win rate, P&L and excursion math. DONE 2026-11-05 (X-15). EV and MAE/MFE checks on the dashboard were removed with decision 0008; the removal is tested.
- **PB-6** MAE/MFE as optimizer parameters (decision 0008). DONE 2026-11-05: `mae_penalty_per_pct` and `mfe_capture_weight` are `Objective` and `EngineConfig` parameters, validated, defaults unchanged, recorded in stats and the trial log. Automatic search over them is OFF until the owner approves bounds and out-of-sample evidence exists.

### P3 - Phase 3 (live path, NOT enabled)
- **P3-1** Broker live feed in `data/ingest.py`. PENDING.
- **P3-2** `execution/openalgo_client.py` adapter. PARTIAL (refuses in paper; real call path PENDING).
- **P3-3** `execution/router.py` and `execution/protection.py`. PENDING.
- **P3-4** Session manager, throttle, live monitor, strategy health, TCA. PENDING.
- **P3-5** Alerts (`notify/alerts.py`) at INFO / WARNING / ERROR / CRITICAL with dashboard feed. PENDING.
- **P3-6** Kill switch and safe mode drills, no orders sent. PENDING.
- **P3-7** Parity test: live paper fills vs replayed backtest. PENDING.
- **P3-8** Regulatory checklist verified against current SEBI rules, from official sources. PENDING. Scope is an open owner question (see "Open owner decisions").

### Docs and governance
- **D-1** Update `AGENTS.md` status line and rule 8 (P&L now shown; Phase 0 done). Rule 8 and the layout comment DONE 2026-11-05 (decision 0008). Status line and full rewrite still PENDING (owner approval).
- **D-2** Review flow doc and `ALGO_FLOW.md`. DONE 2026-11-05: flow doc P&L and event-blackout lines fixed; SVG label fixed; `ALGO_FLOW.md` is only a pointer (no change needed).
- **D-3** Daily SOP entry at `docs/My_trading_system_SOP/SOP.md`. PENDING (file not reviewed).
- **D-4** Work-log entry after each commit (`Temp/Daily work log/`, git-ignored, not pushed). Ongoing.
- **D-5** Doc sync pass (this file, PRIORITY plan, WORKFLOW, STATUS). DONE 2026-11-05. Repeat after each block of work.

---

## Additional priority tasks (added 2026-11-05)

Order: work top to bottom inside each tier. Tier A comes before any capital decision.
Each task ends with tests, a doc update, and a commit. "Needs" says what blocks it.

### Tier A - safety and evidence (before any capital)
- **X-1** Expiry-day and lot-size rules. DONE 2026-11-06 (owner rules): delivery has no expiry; intraday expires the same trading day; lot size applies only to SME stocks (non-SME = 1 share). SME symbol with no lot in `config/calendar/sme_lot_sizes.csv` is refused (fail closed). Code: `core/expiry_rules.py`; tests: `tests/unit/test_expiry_lot_rules.py` (9 pass). Still needs: owner to fill the SME lot table.
- **X-2** Gate-level duplicate-order block after restart (P1-4 remainder). Needs: `openalgo` installed (owner).
- **X-3** Idempotent retry test in the order state machine (P1-3 remainder). DONE 2026-11-05: repeated reports are no-ops, conflicting or stale fills are refused, and a refused move changes nothing (`tests/unit/test_order_fsm_retry.py`). Fixed a bug where a refused FILLED report still changed the fill quantity.
- **X-4** Point-in-time test: conviction inputs and feature store read only past bars (P2-1 exit check, A4). DONE 2026-11-05: late writes, out-of-order writes, inclusive release time, no forward API, and conviction-from-store unchanged by future data (`tests/unit/test_point_in_time_conviction.py`). Store was already correct; tests catch a leaky store (checked with a mutant).
- **X-5** Cost-model tests for STT, brokerage, exchange charges, GST, stamp duty and impact, using the owner's cost rules (commit `4dab074`). DONE 2026-11-05: 23 hand-calculated rupee tests run without OpenAlgo (`tests/unit/test_cost_owner_rules.py`); side and product strings are now validated (a lowercase "sell" used to be charged as a buy); two tautological asserts removed from `test_phase0_wrappers.py` and `test_phase1_research.py`. Rates checked 2026-11-05 against NSE "First time investor" pages (updated 17/04/2026): STT, stamp duty, SEBI turnover fee and GST all match. 2026-11-06: exchange transaction charge updated from 0.00322% to 0.00307% per side (NSE circular FA73061, dated 27 Feb 2026, effective 1 Mar 2026: Rs 307 per crore incl. Rs 0.01 IPFT). Cost tests re-derived by hand; all pass. DONE.
- **X-6** End-to-end paper run with `scripts/run_paper.py` (P1-5). Needs: `openalgo` (owner).
- **X-7** Kill-and-restart test mid-run: no duplicate paper orders (P1 exit check 5). Needs: none for ledger-level; full process needs `openalgo`.
- **X-8** Event blackout wired into entry checks in `risk/controls.py`, with a test that blocks entries on blackout days and allows exits. DONE 2026-11-05: `check_intent` blocks entries on blackout days (per symbol, from `event_blackout_dates`), allows exits and protection orders, and fails closed when `trade_day` is missing for a symbol with a blackout (`tests/unit/test_event_blackout_gate.py`, 9 tests; two planted bugs caught). Open: no runner feeds blackout dates into `RiskState` yet (needs `scripts/run_paper.py`).
- **X-9** Layout test: no import of `core/llm` from `risk/`, `execution/`, `portfolio/` (P2-7). DONE 2026-11-05: AST scan of the trade path (`risk/`, `execution/`, `portfolio/`, `monitor/ledger.py`, 26 files) finds no direct LLM import (`tests/unit/test_no_llm_in_trade_path.py`, scanner checked with a planted import).
- **X-9b** GAP FOUND by X-9: the trade path loads LLM client code indirectly. Any `blue_engine.core.*` import runs `core/__init__.py`, which imports `frontline_agent` and then `core/llm`. Any `blue_engine.portfolio.*` import runs `portfolio/__init__.py`, which imports `views/llm_note.py`. Not called on the trade path, but loaded in the same process. Fix: make these package imports lazy in Blue (the `core/__init__.py` file already has a lazy `__getattr__`). Needs: owner to run `pytest Blue_engine/tests` after the change, because Blue has 84 tests and `openalgo` is missing here. Tracked as an expected failure in the test file; the test fails once fixed, so its marker must then be removed.

### Tier B - research evidence (needs real data)
- **X-10** Real-data OCL backtest with costs, walk-forward, DSR and PBO (P1-6). 2026-11-06: INFY bars are fetched by `Blue_engine/database/sample_data.py` to a git-ignored folder on the owner machine (not in repo; source host unreachable from sandbox). License: personal, non-commercial research only; commercial automated trading needs written permission. PENDING: owner runs the fetch on own machine and confirms the use is personal research.
- **X-11** Out-of-sample conviction calibration report (P2-6). Needs: X-10 data.
- **X-12** Trial log entry for every optimisation run (AGENTS rule: sampler, trial count, bounds, folds, cost model version). DONE 2026-11-05 (wiring): with `EngineConfig.trial_log_path` set, Blue writes one row per trial (failed ones too) and one run row; `TrialLog` reads them (`tests/unit/test_trial_log_optimizer.py`). Default is OFF, so the research runner (X-6/X-10) must set the path. Owner to decide if the default should be on.
- **X-13** Survivorship-free universe and corporate-action review (P1-7). Needs: owner data source for delisted symbols.

### Tier C - dashboard
- **X-14** NOT NEEDED (owner, 2026-11-05). Was: rule-compliance panel and strategy deviation alerts (PB-3 remainder).
- **X-15** Hand-checked tests for EV, profit factor, MAE/MFE on a fixed trade list (PB-5). DONE 2026-11-05: 10 tests with hand-worked numbers (EV 0.6%, win rate 60%, PF 2.0, net P&L 3000, MAE 4%, MFE 10%, short-side flip, edge cases) in `tests/unit/test_dashboard_metrics_handchecked.py`. Four planted bugs caught. MAE/MFE were removed from the dashboard by decision 0008 (2026-11-05); the excursion math is still tested. Also: win rate uses the return sign and profit factor uses the rupee sign; they could disagree if costs flip a trade.
- **X-16** NOT NEEDED (owner, 2026-11-05). Was: upcoming NSE board meetings on the dashboard. The parser and blackout helper stay, because X-8 uses them.

### Tier D - signal build-out (paper only)
- **X-17** Event scoring, advice only, with a deterministic fallback when the LLM is off (P2 events). Needs: none for the fallback; LLM provider is optional. DONE 2026-11-05: `score_filing_advice` uses an optional LLM callable, falls back to the keyword scorer on error or non-finite output (WARNING logged), clamps to [-1, 1], records `source`, and never sets size (`tests/unit/test_event_advice_fallback.py`).
- **X-18** Regime detector and factor models (P2-4). Needs: X-10 data to validate.
- **X-19** Model registry with human sign-off record (P2-5). Needs: owner sign-off process.

### Tier E - governance and docs (owner actions)
- **X-20** Rewrite `AGENTS.md` status line and rule 8 (P&L now shown; Phase 0 done) (D-1). Needs: owner approval.
- **X-21** Review `docs/My_trading_system_SOP/SOP.md` against reality (D-3). Needs: none.
- **X-22** Delete stale remote branches `main` (`76f21af`) and `docs/status-2026-11-05`. Needs: owner (sandbox cannot delete branches).
- **X-23** Verify the 2026 holiday list against the NSE circular (P0-4). 2026-11-06: checked against NSE circular CD71962; 4 dates differ (see P0-4). Needs: owner decision on the 4 dates.
- **X-24** SEBI algo-trading checklist (registration, order tagging, immutable audit store, clock sync) from official sources (P3-8). 2026-11-06: owner confirmed "algo is permitted" (scope: algo trading allowed). Checklist still required before live capital. Needs: owner research; sandbox can reach SEBI via search only.

### Tier F - Blue_engine follow-ups from the Phase 0 audit (added 2026-11-05)
These come from `docs/audits/PHASE0_REUSE_AUDIT.md`. None of them was tracked as an item before this pass.
- **X-25** Blue HRP (`portfolio/optim/hrp.py`) gives different weights from our HRP (`portfolio/optimizer.py`) on the same covariance. Ours matches the published Lopez de Prado algorithm on 25 random covariances, so ours stays local for now. Needs: owner decision (keep local, or fix Blue). A Phase 2 exit check depends on it. Decision note drafted: `docs/decisions/0009-hrp-optimizer-choice.md` (PROPOSED, owner to choose).
- **X-26** Blue `ml/evaluation/metrics.py::deflated_sharpe` mis-scales the threshold (no cross-trial variance). It rejects almost every strategy at N >= 10. Decision 0001 applies: do not use Blue's `deflated_sharpe` in any gate. Needs: owner to fix Blue or confirm local-only; then add a test that gates never import it.
- **X-27** Two Sharpe definitions in Blue: `portfolio/attribution/perf.py::sharpe` (annualised compound return over vol) and `ml/evaluation/metrics.py::sharpe` (arithmetic mean over std). They disagree on the same window, so reports can conflict. Our `strategy_health.rolling_sharpe` is a third definition. Needs: owner to pick one definition for reports and label the others.
- **X-28** Blue_engine tests with the SDK loaded (49 failures, from the 2026-11-03 baseline). Real logic failures: `test_phase3_bars` (expected at least 4 strategy calls, got 2) and `test_optimizer_engine` (best score 2.97 is under 90% of the known peak 3.84). Not yet classified: `test_portfolio_engine`, `test_phase4_registry_cli`. Environment failures (`pyarrow`, `yfinance`) are not bugs. Needs: owner run with `openalgo==2.0.3`, `pyarrow` and `yfinance` installed (see P0-2).
- **X-29** `test_skill_writer::test_default_sandbox_is_under_the_project_data_dir` expects the folder name `my-trading-system`, so it fails in a checkout named `Algo-Pro`. Fix the test so it does not depend on the checkout name. DONE 2026-11-05 for the checkout-name part: the test now checks only that the sandbox is `<repo>/data/agent_stage`. It is skipped in the sandbox (no `openalgo`), so the owner run must confirm it. DONE 2026-11-05: `tests/unit/test_case_paths.py` blocks quoted lowercase `blue_engine/` path literals in `src/` and `Blue_engine/` code. The one real code path (`Blue_engine/core/runner.py`, `DEFAULT_CONFIG`) and two docstring paths were fixed.
- **X-30** Impact: the code uses a flat 5 bps. Blue has a square-root impact model (`impact_coefficient`), which is not used. Phase 1 exit check 3 wants impact included. Switching changes backtest results, so it needs its own commit and test update. Needs: owner decision. Decision note drafted: `docs/decisions/0010-impact-model-choice.md` (PROPOSED).

### Next to start without the owner
- Done 2026-11-05 without owner input: X-12 (wiring), X-17 (fallback), X-29 (case check), PB-6 (MAE/MFE optimizer parameters), decision notes 0009 and 0010 (PROPOSED).
- Still needs the owner: P0-2, P0-3, P0-4, X-9b test run, X-10 data, the 0009/0010 choices, the trial-log default, and the MAE/MFE search bounds.

---

## Open owner decisions

- **SEBI checklist scope (conflict to resolve).** `docs/STATUS_2026-11-04.md` says SEBI algo-rule verification was "removed from scope at your request". The 2026-11-05 plan still requires P3-8 and X-24 (SEBI checks) before any real capital. Owner to say which one applies. Until then, the plan keeps the checks as required.
- Risk limit values (P0-3). Starter values only.
- Tier values and weights (`config/conviction_tiers.yaml`). Placeholders until out-of-sample evidence exists.
- Holiday list (P0-4, X-23). BSE dates still to confirm.
- "Authorised" accounting view method (PB-4).
- Impact model (X-30) and HRP choice (X-25). Drafts: `0010`, `0009`.
- Trial log on or off by default (`EngineConfig.trial_log_path`, X-12).
- Search bounds for the MAE/MFE weights (PB-6). Off until approved.

## Open gaps and assumptions

- Expiry days and lot sizes: rules done 2026-11-06 (X-1). SME lot table still empty (owner to fill).
- Risk limit values: owner approved the starter minimums for PAPER on 2026-11-06. Live values are not approved.
- Paper period length, parity tolerance and starting capital are not set.
- Event blackout uses the NSE board-meeting feed. It is a blocking input only, not a signal. The sandbox cannot reach NSE from code; the sample used in tests is a saved fixture.
- Blue_engine reuse targets are named by path. Read and confirm each before wrapping.
- `AGENTS.md` rule 8 updated 2026-11-05 (decision 0008). The full rewrite (X-20) is still pending.
- The 107 current failures are the same set as before this change. Most are `openalgo` missing. The Blue_engine logic failures in X-28 are real and not yet fixed.