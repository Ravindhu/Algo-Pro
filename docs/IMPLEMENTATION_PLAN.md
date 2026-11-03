# Implementation plan: quant system flow v5

Status: plan only. Every module under `src/quant/` is a scaffold. No trade logic is built yet.
Source of truth: `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg`.
Layout: `AGENTS.md` (project layout) and `src/quant/`.

Rules for every phase:
- A stage is live only when its code, unit tests and exit checks pass.
- Nothing in Phase 1 or 2 may reach OpenAlgo.
- Only `execution/openalgo_client.py` may call the order API.

---

## Phase 1: Safe foundation (research and paper only)

Goal: run the pipeline end to end on paper, with no capital.
Timeline: not set. You decide.

| Flow lane | Blocks | Module |
|---|---|---|
| Research validation | Historical bars, Replay simulator | `research/backtest.py` |
| Research validation | Data QA gate | `data/qa.py` |
| Research validation | Indian cost model | `research/costs.py` |
| Research validation | Walk-forward + purged CV | `research/walk_forward.py` |
| Research validation | Trial logger + DSR/PBO, Bootstrap of trades | `research/trial_log.py`, `research/validation.py` |
| Research validation | Experiment lineage | `research/lineage.py` |
| Data | Market data live, Feature store | `data/ingest.py`, `data/feature_store.py` |
| Data | Universe and actions | `data/universe.py`, `data/corp_actions.py` |
| Safety gate | Trade intent, Risk gate | `risk/gate.py` |
| Safety gate | Pre-trade checks | `risk/pretrade.py` |
| Safety gate | Price band + ASM/GSM | `risk/price_band.py` |
| Safety gate | Liquidity cap | `risk/liquidity.py` |
| Safety gate | Loss budget breaker | `risk/loss_breaker.py` |
| Support | Kill switch | `risk/kill_switch.py` |
| Support | Safe mode controller | `monitor/safe_mode.py` |
| Audit | Audit log, Override log | `audit/log.py`, `audit/override_log.py` |
| Execution | Approved order queue, Order state machine | `execution/queue.py`, `execution/order_fsm.py` |
| Post-trade | Ledger and recon, Restart and recovery | `monitor/ledger.py` |
| Gap | Paper trading stage | `scripts/run_paper.py` (named in AGENTS.md, not created) |
| Gap | Trading calendar | `config/calendar/` (data only, no loader yet) |

Reuse (wrap, do not copy):
- `Blue_engine/core/risk_gate.py`, `Blue_engine/core/kill_switch_store.py`
- `Blue_engine/core/paper_broker.py`, `Blue_engine/core/paper_shift.py`
- `Blue_engine/core/ledger.py`, `Blue_engine/core/ledger_store.py`
- `Blue_engine/backtest/costs.py`, `Blue_engine/backtest/engine.py`, `Blue_engine/backtest/loops.py`
- `Blue_engine/ml/evaluation/walk_forward.py`, `Blue_engine/ml/audit/leakage.py`

Exit checks:
1. Each hard limit in `config/risk_limits.yaml` has a unit test that blocks an intent. The limits are empty now and you must set them.
2. Every intent writes an audit record and a ledger row with an intent ID.
3. Backtest costs include STT, brokerage, exchange charges, GST, stamp duty and impact.
4. The trial log counts every trial. DSR and PBO are computed. A point-in-time test shows no look-ahead.
5. Kill the process mid-run, restart, and confirm no duplicate paper orders.

---

## Phase 2: Signal, conviction, sizing and portfolio (still paper only)

Goal: produce target positions that respect caps. Models enter only after a human signs off.
Depends on: all Phase 1 exit checks passing.

| Flow lane | Blocks | Module |
|---|---|---|
| Market inputs | India flow data, Bulk and block deals, Relative strength, Intraday microstructure, Filings and circulars, Global cues | `data/market_inputs.py` |
| Models | Regime detector | `models/regime.py` |
| Models | Factor and alpha models | `models/factors.py` |
| Signal and conviction | Event scoring (advice only), Signal, Meta-labeler, Calibrated conviction | `models/events.py`, `models/signal.py`, `models/meta_label.py`, `models/calibration.py` |
| Signal and conviction | Liquidity screener | `risk/liquidity.py` |
| Research | Capacity estimator | `research/capacity.py` |
| Sizing | Fractional Kelly | `portfolio/sizing.py` |
| Sizing | Capital allocator, Cash and margin manager | `portfolio/allocator.py` |
| Sizing | HRP optimizer, Covariance, shrinkage | `portfolio/optimizer.py`, `portfolio/covariance.py` |
| Sizing | Drift check, Rebalancer | `portfolio/rebalance.py` |
| Portfolio risk | Portfolio risk, Stress and CVaR, Beta limits | `portfolio/portfolio_risk.py` |
| Improvement loop | Champion/challenger, Model registry, Human sign-off | `feedback/champion_challenger.py` |
| Improvement loop | Re-opt request, Re-run research | `feedback/reopt.py` |
| Monitor | Data drift monitor, Live vs backtest parity | `monitor/drift.py`, `monitor/parity.py` |

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

| Flow lane | Blocks | Module |
|---|---|---|
| Session guards | Market-halt handler, Event blackout, Overnight gap check | `risk/controls.py` |
| Session guards | Exit manager, Square-off and rollover | `risk/exits.py`, `risk/squareoff.py` |
| Live risk | Live intraday VaR/CVaR | `portfolio/portfolio_risk.py` |
| Execution | Algo router, Protection, OpenAlgo | `execution/router.py`, `execution/protection.py`, `execution/openalgo_client.py` |
| Feeds | Broker feeds, Feed health monitor | `data/ingest.py` |
| Support | Broker session manager, Order-rate throttle | `monitor/session.py`, `monitor/throttle.py` |
| Post-trade | Live monitor, Strategy health, TCA to cost model | `monitor/live_monitor.py`, `monitor/strategy_health.py`, `monitor/tca.py` |
| Reporting | Dashboard, Tax lots | `reporting/dashboard.py`, `reporting/accounting.py` |
| Notify | Alerts | `notify/alerts.py` |

Reuse (wrap, do not copy):
- `Blue_engine/core/client.py`, `Blue_engine/core/broker_auth.py`, `Blue_engine/core/order_updates.py`
- `Blue_engine/core/quote_health.py`, `Blue_engine/core/throttle.py`
- `Blue_engine/core/exit_engine.py`, `Blue_engine/core/protective_orders.py`, `Blue_engine/core/auction_windows.py`
- `Blue_engine/core/live_monitor.py`, `Blue_engine/core/lot.py`
- `Blue_engine/dashboard/app.py`. Check `pov_state_machine.py` first: it is dashboard state, not an order router.

Exit checks (all required before real capital):
1. Live paper fills match the replayed backtest within a tolerance you set.
2. The regulatory items in the flow doc (SEBI algo registration and order tagging, immutable audit store, NTP clock sync) are checked against current SEBI rules. This plan did not verify them.
3. Capital starts at a fixed, small cap set in config. Any scale-up needs your sign-off.
4. The kill switch and safe mode are tested in a live session with no orders sent.
5. The dashboard shows P&L hidden by default, with EV, MAE/MFE and rule compliance visible.

---

## Open gaps and assumptions

- Gaps: Paper trading stage and Trading calendar have no module yet (see `KNOWN_GAPS` in `tests/unit/test_layout.py`). `src/quant/data/` is now scaffolded (Phase 1, modules only, no logic).
- Risk limit values are unset in `config/risk_limits.yaml`. Live trading stays blocked until you set them.
- Paper period length, parity tolerance and starting capital are not set here.
- Blue_engine reuse targets are named by path only. Read and confirm each one before wrapping it.
