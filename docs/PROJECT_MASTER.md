# Quant Trading System - Master Project Document

Owner: Ravindhu
Repo: https://github.com/Ravindhu/Algo-Pro
Created: 2026-11-07
Based on: `docs/STATUS_2026-11-06.md` (latest status log)

**Current mode: PAPER ONLY. Nothing is live. No order reaches OpenAlgo.**

---

## 0. How to use this file

- This is the single entry point for plan and status.
- Older planning, status and audit files are NOT changed by this file. They stay where they are as records.
- Decisions are in `docs/decisions/`. If this file and a decision record disagree on a decision, the decision record wins.
- Update this file in the same commit as any change to status, plan or priorities.

---

## 1. Objective and hard rules

**Goal:** a quant system for NSE and BSE equities that protects capital first, executes consistently second, and aims for profit third.

| # | Rule | Where it is enforced |
|---|---|---|
| R1 | Every order intent passes the risk gate. No other stage reaches OpenAlgo. | `risk/money_checks.py`, `execution/openalgo_client.py` |
| R2 | No AI or LLM stage on the trade path. Event scoring is advice only. | `tests/unit/test_no_llm_in_trade_path.py` |
| R3 | Conviction scores never override hard caps. Size = min(all caps). | `portfolio/tiers.py` |
| R4 | `src/quant/` must not import `Blue_engine/` at the end state. See section 4. | Target, not yet enforced |
| R5 | No strategy is trusted without out-of-sample and paper-trading evidence. | Trial log, DSR/PBO |
| R6 | P&L is shown by default. A toggle can hide it on screen. P&L never sets size. | Decision 0007 |
| R7 | Costs are always in the test: STT, brokerage, exchange charge (0.00307% per side, NSE circular FA73061), GST, stamp duty, impact. | `research/costs.py` |
| R8 | Live trading needs a `LiveApproval` tied to the current risk-limits hash, plus an approved gate decision. | `execution/openalgo_client.py` |
| R9 | Each change keeps the docs in sync in the same commit. | This file |

---

## 2. Current status (as of 2026-11-06 log)

**Done (code and tests exist):**
- Expiry and lot-size rules (SME lot size read from broker; whole lots only). Decision 0016.
- Trading calendar and holiday loader (`config/calendar/holidays.csv`, 16 equity holidays from NSE circulars).
- Conviction score and size tiers (tiers `enabled: false` until owner approves values).
- Cap wiring (tier multiplier, then min of all caps).
- Walk-forward splitter, momentum factors, cost model (local, with parity tests).
- Covariance, CVaR, rebalance rule, Kelly and vol-target sizing (local, parity tested).
- Money risk checks (`risk/money_checks.py`), parity with Blue on 34,560 grid cases. Decision 0018.
- Local `.env` loader (`core/env.py`). Decision 0019.
- Live guard in the OpenAlgo client. Decisions 0017 and 0020.
- Kill switch: manual (dashboard, token and reason required) and automatic on daily loss (`risk/kill_close.py`, `risk/auto_kill.py`). Decision 0020.
- Dashboard read and write (`/ui`), with limits and kill-switch controls.
- P&L shown by default. EV, MAE/MFE and rule compliance removed from the dashboard. Decisions 0007 and 0008.
- Champion/challenger model registry sign-off test.
- Screener rules (`data/screen_rules.py`, 826 parity cases).
- Override log and ledger restart tests.
- Event-blackout helper and NSE event-calendar parser (no network call in code).

**Not done:**
- Signal, meta-labeler, regime and factor models (beyond momentum). Blocked by validation data (X-10).
- Calibration report (X-11). Depends on X-10.
- INFY backtest is test data only. `config/data_sources.yaml` marks it `live_allowed: false`. A licensed live feed is not chosen yet.
- A runner in `src/quant/` that runs the whole flow end to end. **No runner exists yet.**
- HRP optimizer still calls Blue (section 4).
- Blue-only features: screener data, economics, full optimizer, POV engine, agent pipeline.

**Test state (sandbox, 2026-11-06):**
- `pytest tests/unit tests/parity`: 1332 passed, 15 skipped, 27 failed.
- All 27 failures need `httpx`, which the sandbox cannot install (PyPI blocked). They are in the HRP path.
- Owner machine run (`pip install -r requirements.txt`, then pytest) is still pending.

---

## 3. Trade-flow stages: status

Source: `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg`. Status is taken from the status logs and was not re-run in this pass.

| Lane | Stage | Status | Location / note |
|---|---|---|---|
| Research | Data QA gate | Partial | `data/qa.py` |
| Research | Walk-forward + purged CV | Done (local) | `research/walk_forward.py` |
| Research | Trial logger + DSR/PBO | Partial | `research/` (see decision 0001) |
| Research | Cost model | Done (local) | `research/costs.py` |
| Research | Covariance + shrinkage | Done (local) | `portfolio/covariance.py` |
| Research | Capacity estimator | Exists | `research/capacity.py` |
| Research | Lineage | Exists | `research/lineage.py` |
| Market inputs | Event calendar parser | Done (parser) | `data/market_inputs.py` |
| Market inputs | FII/DII, bulk deals, microstructure | Not built | - |
| Data | Feature store, universe, corp actions | Exists | `data/feature_store.py`, `universe.py`, `corp_actions.py` |
| Data | Regime detector | Not built | `models/regime.py` is a scaffold |
| Signal | Factor models | Partial | `models/factors.py` (momentum done) |
| Signal | Signal + meta-labeler | Not built | Needs X-10 |
| Signal | Conviction score | Done | `models/conviction.py` |
| Sizing | Fractional Kelly, vol target | Done (local) | `portfolio/sizing.py` |
| Sizing | Size tiers | Done, disabled | `portfolio/tiers.py` |
| Sizing | HRP optimizer | Partial, Blue call | `portfolio/optimizer.py` (section 4) |
| Sizing | Rebalancer | Done (local) | `portfolio/rebalance.py` |
| Portfolio risk | Sector/cluster caps, CVaR | Partial | `risk/`, `portfolio/portfolio_risk.py` |
| Session guards | Trading calendar | Done | `core/calendar.py` |
| Session guards | Event blackout | Helper done, runner wiring open | `risk/controls.py` |
| Session guards | Exit manager, square-off | Exists | `risk/exits.py`, `risk/squareoff.py` |
| Safety gate | Pre-trade checks, money limits | Done | `risk/money_checks.py`, `risk/pretrade.py` |
| Safety gate | Price band, liquidity cap, loss breaker | Exists | `risk/price_band.py`, `liquidity.py`, `loss_breaker.py` |
| Execution | OpenAlgo client with live guard | Done | `execution/openalgo_client.py` |
| Execution | Queue, order state machine, protection | Partial | `execution/queue.py`, `protection.py` |
| Execution | POV engine | Blue only | Stage 6.5 |
| Post-trade | Ledger, TCA, live monitor | Partial | `monitor/ledger.py`, `tca.py`, `drift.py` |
| Post-trade | Kill switch | Done | `risk/kill_close.py`, `risk/auto_kill.py` |
| Improvement | Champion/challenger, registry | Done (sign-off test) | `feedback/` |
| Support | Safe mode, throttle, dashboard | Done / partial | `monitor/safe_mode.py`, `monitor/throttle.py`, `reporting/dashboard.py` |
| Support | Audit log, override log | Done | `audit/log.py`, `audit/override_log.py` |
| Support | Alerts | Exists | `notify/alerts.py` |

---

## 4. Making the quant flow run without Blue_engine

### 4.1 Where we are

- All Blue imports in `src/quant/` go through one gate: `src/quant/core/blue_bridge.py`.
- A search for the bridge name found one runtime caller in `src/quant`: `src/quant/portfolio/optimizer.py` (HRP).
- Done and moved off Blue: stages 1, 2, 4, and items 6, 7, 9, 10, 11, 12 of stage 3.
- Still on Blue: HRP (item 8), and all of stage 6 (screener data, economics, full optimizer, POV engine, agent pipeline).
- **The live POV launcher still runs Blue's runner.** `scripts/run/run_pov.py` calls `blue_engine.core.runner.run_with_config`. So today, trades can only run through Blue's code.

Note: the search covered file names and key import text. Each import line has not been checked one by one. Step 1 below does that.

### 4.2 Remaining Blue dependencies

| # | Dependency | Where | Blocker | Stage |
|---|---|---|---|---|
| B1 | HRP optimizer | `src/quant/portfolio/optimizer.py` via `blue_bridge` | Owner choice D1 (decision 0009) | 3.8 |
| B2 | Screener data: screener.in client, PDF parsing, yfinance, valuations (about 6,800 lines) | `Blue_engine/screener/` | Owner data access; network needed | 6.2 |
| B3 | Economics | `Blue_engine/economics/` | None known | 6.3 |
| B4 | Full optimizer (beyond HRP) | `Blue_engine/optimizer/`, `portfolio/optim/` | After B1 | 6.4 |
| B5 | POV engine | `Blue_engine/pov/`, `scripts/run/run_pov.py` | After runner exists | 6.5 |
| B6 | Agent pipeline (advice only, never trade path) | `Blue_engine/alpha/`, `core/llm/` | Last | 6.6 |
| B7 | Runner and entry scripts | `scripts/run/run_pov.py`, `scripts/ops/boot_sequence.py`, `scripts/run/run_supervised.py`, `scripts/run/run_economic_engine.py`, `scripts/run/run_screener_bridge.py`, `scripts/verify/*`, `scripts/train/*` | Re-point each to `src/quant` or retire | Stage 5 |
| B8 | Installers and requirements | `requirements.txt`, `install.bat`, `install.sh`, `Servers/install_ubuntu.sh` | After stage 6 | Stage 5 |
| B9 | Unit tests that import Blue | `tests/unit/test_phase0_*`, `test_excursion_weights.py`, `test_trial_log_optimizer.py` | Rewrite with local code, or move to `tests/parity/` | Stage 5 |
| B10 | No quant runner | `src/quant/` | Needs at least one signal (D5) | Stage 4 |

### 4.3 Steps to zero Blue dependency

Each step has an exit check. Do not start a step until the previous one is done.

**Step 1 - Full import audit.** Check every import line in `src/quant/` and every entry script. Confirm nothing else calls `blue_engine`. Record the result in section 4.2.
- Exit check: list recorded. No unknown callers.

**Step 2 - Close HRP (B1).** After owner choice D1, port `hrp_weights` into `src/quant/portfolio/` with no Blue import. Add a golden test from the owner machine (D4). Switch `optimizer.py` to the local code.
- Exit check: `optimizer.py` has no `blue_bridge` call. HRP output matches the chosen variant.

**Step 3 - Build the quant paper runner (B10).** Add `src/quant/runner.py` (or `scripts/run/run_quant_paper.py`). It uses only `src/quant` modules: data, features, signal, conviction, sizing, risk, gate, OpenAlgo client in paper mode. First signal is set by D5.
- Exit check: runner runs on sample data in paper mode. No Blue import in its path.

**Step 4 - Port Blue-only features, one at a time (stage 6).** Order: 6.2 screener data (split into smaller sub-steps), 6.3 economics, 6.4 optimizer, 6.5 POV engine, 6.6 agent pipeline. For each: move code into `src/quant`, move the matching Blue tests, add parity or golden tests, update the flow doc line and the SVG mapping.
- Exit check per sub-step: tests pass (or the owner-only data test is marked). No `blue_engine` import in that feature.

**Step 5 - Add a guard test.** Make a test fail if `blue_engine` or `blue_bridge` appears in `src/quant/` or in trade-path entry scripts. Keep parity tests in `tests/parity/`, which the owner runs with Blue present.
- Exit check: test is green. It would fail on any new Blue import.

**Step 6 - Remove Blue (stage 5).** Only after steps 2 to 5 are done and the owner accepts the parity results. Move `Blue_engine/` to `Temp/archive/<date>/Blue_engine/`. Remove Blue from requirements and installers. Update AGENTS.md, README and the SOP.
- Exit check: paper run works from a clean checkout. `pytest tests/unit` passes except owner-data tests.

### 4.4 Test kinds used for decoupling

| Kind | Where it runs | Purpose |
|---|---|---|
| Hand-checked | Everywhere | Fixed numbers worked out by hand |
| Parity (live Blue) | Where Blue loads (owner, and sandbox for some items) | Exact match with Blue |
| Golden (JSON from Blue) | Everywhere, once fixtures exist | Match with Blue without `openalgo` installed |

Rule: a Blue replacement is accepted only when the difference is zero, or the tolerance is written down and approved (rel 1e-9 for formulas; weights within 1e-6 for optimizers).

---

## 5. Priorities for the next session

1. **Close the last runtime Blue call in `src/quant/`: HRP (Step 2).** Needs owner choice D1 first. If D1 is not decided, prepare the golden fixture plan so the port is ready.
2. **Build the quant paper runner (Step 3).** Needs owner choice D5 (first signal). After this, the flow can run without `Blue_engine`.

Blocked until owner acts: D4 (golden export on owner machine) and D3 (sign-off on risk gate port).

---

## 6. Owner decisions (open)

| ID | Decision | Why it matters |
|---|---|---|
| D1 | HRP variant (decision 0009) | Needed before HRP port |
| D3 | Sign-off on the risk-gate port (decision 0018) | Risk-critical code |
| D4 | Run `export_blue_goldens.py` on owner machine | Golden fixtures |
| D5 | First signal to port into the runner (keylevel is simplest; OCL pattern next) | Unblocks runner |
| D6 | Holiday segments: 4 dates in the NSE circular are not added (2026-02-19, 03-19, 04-01, 08-26) | Calendar accuracy |
| D7 | Muhurat session 2026-11-08 (Sunday): record or skip | Calendar accuracy |
| D8 | Live data source: licensed feed (INFY is test only) | Live trading blocked |
| D9 | Personal-use license for data; commercial automated trading needs written permission | Legal |
| D10 | Accounting view: password-gated, confirm design | Access control |
| D11 | Tier values and weights are placeholders until out-of-sample evidence | Sizing |
| D12 | Delete remote `main` (76f21af) and `docs/status-2026-11-05` on GitHub | Repo cleanup |

---

## 7. Module layout (`src/quant/`)

| Flow lane | Package |
|---|---|
| Research validation | `src/quant/research/` |
| Market inputs | `src/quant/data/market_inputs.py` |
| Data and features | `src/quant/data/` |
| Signal and conviction | `src/quant/models/` |
| Sizing and portfolio | `src/quant/portfolio/` |
| Portfolio risk, session guards, safety gate | `src/quant/risk/` |
| Execution | `src/quant/execution/` |
| Post-trade | `src/quant/monitor/` |
| Audit | `src/quant/audit/` |
| Improvement loop | `src/quant/feedback/` |
| Support, dashboard, alerts | `src/quant/monitor/`, `src/quant/reporting/`, `src/quant/notify/` |
| Shared core | `src/quant/core/` (includes `blue_bridge.py`, to be removed at Step 6) |
| Vendored OpenAlgo SDK fallback | `src/quant/vendor/` (decision 0014) |

---

## 8. Document map (existing files, kept as they are)

| File | Use it for |
|---|---|
| `AGENTS.md` | Rules for AI agents working in this repo |
| `README.md`, `INSTALL.md` | Setup and install |
| `docs/quant_system_flow_v5.md` + `.svg` | Full trade-flow design |
| `docs/DECOUPLING_PLAN.md` | Detailed stages 1 to 6 of the Blue decoupling |
| `docs/PRIORITY_IMPLEMENTATION_PLAN.md` | Conviction sizing and dashboard P&L work |
| `docs/IMPLEMENTATION_PLAN.md` | Full item list (IDs P0, P1, X-*, etc.) |
| `docs/LIVE_TRADING_IMPLEMENTATION_PLAN.md` | Path to live trading |
| `docs/STATUS_2026-11-06.md` | Latest status detail |
| `docs/decisions/` | One decision per file (0001 to 0026) |
| `docs/audits/` | Dated audit reports |
| `docs/My_trading_system_SOP/` | Owner SOP |
| `docs/skills/` | Skill notes |
| `rules/token-optimization.md` | Token-use rules |

---

## 9. Change log

- 2026-11-07: Created this file. Added the Blue decoupling path (section 4) and the next-session priorities. No existing file changed.
- 2026-11-06: Stage 4 done (money checks, env loader). Stage 6.1 screener rules done. Hardening decisions 0017 and live guard.
- 2026-11-05: Conviction, tiers, calendar merged. P&L shown by default. Decision 0012 (keep Blue features inside quant).
