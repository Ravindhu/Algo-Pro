# Quant folder plan: what exists, what is wrapped, what is new

Status: created 2026-11-05. Docs only. No code changed.
Status source: `docs/IMPLEMENTATION_PLAN.md` and `docs/STATUS_2026-11-05.md`.
This file maps the folders in `src/quant/` to the plan. It does not replace those docs.

---

## 1. Short answer: is the wrapping work done?

Partly.

**Wrapped from `Blue_engine/` and done (tests pass in the sandbox):**
- Fractional Kelly sizing: `portfolio/sizing.py`
- Covariance shrinkage: `portfolio/covariance.py` (decision 0003)
- Drift check and rebalancer: `portfolio/rebalance.py`

**New code and done:**
- Conviction score: `models/conviction.py` (six conditions)
- Size tiers and cap wiring, disabled by default: `portfolio/tiers.py`
- Event blackout entry block: `risk/controls.py` (X-8)
- Order state machine retry safety: `execution/order_fsm.py` (X-3)
- Point-in-time test for conviction inputs (X-4), layout test that keeps LLM out of the trade path (X-9)

**Wrapped but still partial:**
- Cost model: `research/costs.py` (rule tests done; exchange charge rate still open)
- Backtest, walk-forward, trial log, DSR and PBO, lineage: `research/*` (need real NSE data)
- Queue, ledger, risk gate, kill switch, safe mode: `execution/*`, `monitor/*`, `risk/*`

**Kept local on purpose (Blue differs or is not trusted):**
- HRP optimizer `portfolio/optimizer.py` (Blue HRP gives different weights, X-25)
- Regime detector `models/regime.py` (decision 0005)
- Deflated Sharpe: do not use Blue's `deflated_sharpe` in any gate (X-26)
- Impact: code uses a flat 5 bps; Blue has a square-root model not used yet (X-30)

**Still new code, not started:**
- Signal, meta-labeler, event scoring: `models/signal.py`, `models/meta_label.py`, `models/events.py`
- Router and protection: `execution/router.py`, `execution/protection.py`
- Broker live feed `data/ingest.py`, session manager, throttle, parity, drift
- Alerts `notify/alerts.py`, feedback loop `feedback/*`

So the flow has a working skeleton and tested guards. Strategy and live parts are not built yet.

---

## 2. Folder map (`src/quant/`)

Each folder has a `README.md` with its purpose and status.

| Folder | Flow lane | Done | Partial | Pending |
|---|---|---|---|---|
| `core/` | shared basics | `calendar.py` (2026 holidays) | `blue_bridge.py`, `config.py`, `types.py` | expiry-day rules (X-1) |
| `data/` | market inputs | `market_inputs.py` (NSE event parser, blackout dates) | `qa.py`, `feature_store.py`, `universe.py`, `corp_actions.py` | `ingest.py` live feed; bulk/block deals (P2-8) |
| `research/` | research validation | cost rule tests | `backtest.py`, `walk_forward.py`, `trial_log.py`, `validation.py`, `lineage.py` | `capacity.py` |
| `models/` | signal and conviction | `conviction.py` | `calibration.py`, `factors.py` | `regime.py`, `signal.py`, `meta_label.py`, `events.py` |
| `portfolio/` | sizing and allocation | `sizing.py`, `tiers.py`, `rebalance.py`, `covariance.py` | `allocator.py`, `optimizer.py`, `portfolio_risk.py` | HRP decision (X-25) |
| `risk/` | safety gate | `controls.py` entry blackout (X-8) | `gate.py`, `pretrade.py`, `price_band.py`, `liquidity.py`, `loss_breaker.py`, `kill_switch.py`, `exits.py`, `squareoff.py`, `blue_risk_gate.py` | expiry and rollover (X-1) |
| `execution/` | orders (Phase 3 only) | `order_fsm.py` retry-safe (X-3) | `queue.py`, `openalgo_client.py` (refuses in paper) | `router.py`, `protection.py` |
| `monitor/` | post-trade | `ledger.py` restart (P1-4) | `safe_mode.py` | `live_monitor.py`, `strategy_health.py`, `tca.py`, `parity.py`, `drift.py`, `throttle.py`, `session.py` |
| `audit/` | audit trail | `override_log.py` | `log.py` (tamper test) | immutable store check (P3-8) |
| `feedback/` | improvement loop | - | - | `reopt.py`, `champion_challenger.py` (needs sign-off, X-19) |
| `notify/` | alerts | - | - | `alerts.py` (P3-5) |
| `reporting/` | dashboard, accounting | `accounting.py` (password view) | `dashboard.py` | **P&L and dashboard rule conflict: open owner decision (section 4)** |

Note: the plan docs give the status. Before editing any file, open it and confirm the status.

---

## 3. Build order (money-flow order)

Follow `docs/IMPLEMENTATION_WORKFLOW.md`. Each step must pass its gate first.

0. Owner decisions and test baseline (owner: P0-2, P0-3, P0-4)
1. Research validation: costs, point-in-time, real-data backtest (owner: NSE bars)
2. Data and features: QA gate, feature store, market inputs
3. Models and signal (advice only; models need sign-off)
4. Conviction, sizing, allocation (conviction never overrides a hard cap)
5. Portfolio risk and session guards
6. Safety gate: the only path to orders
7. Execution (paper first; live not enabled)
8. Post-trade: ledger, TCA, health, parity
9. Improvement loop (human sign-off on every change)
10. Support, dashboard, alerts, audit
11. Live entry (owner sign-off only; small fixed cap)

---

## 4. Open owner decision: P&L and dashboard rules conflict (resolve first)

Three sources disagree. The code must follow one of them. No code was changed for this.

| Source | P&L default | EV, MAE/MFE, profit factor | Rule-compliance panel |
|---|---|---|---|
| `docs/decisions/0007-pnl-shown-by-default.md` (2026-11-05) | Shown | Shown (PB-3, X-15) | Not needed (X-14) |
| `AGENTS.md` rule 8 (commit `a0c7847`, latest on master) | Hidden | Remove from dashboard | Remove |
| Operating brief in the project preferences | Hidden | Show (EV, profit factor, MAE, MFE, risk-to-reward) | Show (rule compliance) |

Owner to answer:
1. Is P&L shown or hidden by default?
2. Should EV, MAE/MFE and profit factor stay on the dashboard?
3. Should the rule-compliance panel exist? (Decision 0007 says not needed; the brief says show.)

Until this is answered, `reporting/dashboard.py` and the dashboard HTML must not be changed for these items. Decision 0007 and `AGENTS.md` rule 8 should be updated in the same commit once the owner answers.

---

## 5. What can be done now without owner input

1. **X-17** Deterministic fallback for event scoring in `models/events.py` (no LLM needed).
2. **X-12** Wire trial-log entries into `research/trial_log.py` for every run (wiring needs no data).
3. **X-29 (case check)** Fix the `blue_engine` vs `Blue_engine/` path check.
4. Draft decision notes for X-25 (HRP) and X-30 (impact). The owner makes the choice.

Blocked by owner or data:
- P0-2: install `openalgo` and run the full suite (sandbox cannot install it; 70 tests fail here for that reason only)
- P0-3: approve risk limits in `config/risk_limits.yaml`
- P0-4 / X-23: verify 2026 holidays against the NSE circular
- X-10: real NSE bar data for backtest, DSR, PBO
- X-9b: a fix in Blue needs an owner test run first
- X-24 / P3-8: SEBI checklist. Scope conflict is still open (see `IMPLEMENTATION_PLAN.md`).
- Section 4: P&L and dashboard rules.

---

## 6. Rules for every change (from `AGENTS.md` and the workflow doc)

1. Find the flow stage and plan ID. If none fits, stop and ask.
2. Read the existing `Blue_engine/` code first. Wrap it, do not copy it.
3. Write code and unit tests (pass and block paths).
4. Run `pytest tests/unit Blue_engine/tests` and record counts.
5. Update docs in the same commit (plan status, flow doc, `docs/STATUS_<date>.md`).
6. Nothing reaches OpenAlgo before Phase 3. Only `execution/openalgo_client.py` may call the order API.
7. `risk/`, `execution/`, `portfolio/` never import `core/llm`.

---

## 7. Limits of this session

- The sandbox cannot install `openalgo` (PyPI blocked), so the full test suite cannot run here.
- The sandbox cannot reach NSE, so real-data work waits for owner data.
- This change adds docs and README files only.
