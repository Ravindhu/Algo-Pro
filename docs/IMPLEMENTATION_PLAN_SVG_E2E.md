# Implementation plan: SVG end-to-end, phases 1 to 5

Status date: 2026-11-09. Branch: `main` (master merged in, no force push).
Written in simple English. Each phase has a status, the evidence, and what is still open.

## 0. Corrections to the earlier summary

The earlier inventory was wrong in places. This plan replaces it. Checked by grep and by running tests, not by memory.

- The strategy-to-intent bridge IS built. `signal/build_signals.py` writes the signals file and `scripts/run_paper.py` reads it.
- Many stages listed as missing are present: tax lots, replay, TCA, drift, parity, strategy health, reconciliation, recovery, capacity, costs, validation, settlement (T+1), FII/DII inputs, filings.
- Real gaps confirmed in code:
  1. Conviction and tiers were not used in sizing.
  2. Event blackout dates were not fed into the runner, and intents had no `trade_day`.
  3. Global cues, bulk/block deals, and the hedge overlay have no build code.

## 1. Verified inventory

| Stage | Status | Evidence |
|---|---|---|
| Strategy to intents to `signals.csv` (paper) | Built | `signal/build_signals.py`, `scripts/run_paper.py`, `tests/test_signal_chain.py` |
| Optimiser (HRP, risk parity, others) | Built | `portfolio/optimiser_*.py`, parity tests |
| Conviction score (six conditions) | Built | `models/conviction.py`, `tests/test_conviction*.py` |
| Conviction tiers in sizing | Built in phase 1, tiers OFF in config | `build_signals._tiered_qty`, `tests/test_phase1_bridge.py` |
| Event blackout feed | Built in phase 1 | `data/market_inputs.read_event_calendar`, `run_paper --events`, `tests/test_phase1_bridge.py` |
| Risk gate, kill switch, safe mode, halt | Built | `risk/*`, `tests/test_phase1_risk_exec.py`, `tests/test_phase3.py`, `tests/unit/test_halt_all_kill_close.py` |
| Duplicate intent block across restart | Built | `tests/test_phase1_gaps.py::test_ledger_remembers_intent_ids_after_restart` |
| Kill-restart end to end | Partly | Kill switch and ledger survive restart (tested). No full kill-restart run of the paper runner. |
| Recovery of a dead process | Built | `monitor/recovery.py`, `tests/test_recovery.py` |
| Reconciliation | Built | `monitor/recon_job.py`, `tests/unit/test_recon_job.py` |
| TCA, strategy health, drift, parity monitors | Built (files present) | `monitor/tca.py`, `strategy_health.py`, `drift.py`, `parity.py`. Quality not audited. |
| Alerts | Built | `notify/alerts.py` |
| Funds and T+1 settlement | Built | `tests/unit/test_settlement_t1.py`, `tests/test_gate_halt_funds.py` |
| Tax lots (FIFO) | Built | `reporting/accounting.py`, `fifo_pnl` in `run_paper.py` |
| Replay | Built (script) | `scripts/research/e2e_replay.py`, `tests/replay` |
| Capacity, costs, validation | Built (files present) | `research/capacity.py`, `costs.py`, `validation.py` |
| Beta | Partly | Present in `portfolio/portfolio_risk.py`. Enforcement in the gate not verified. |
| Feed health (reconnect, gaps, heartbeat) | Partly | `data/reconnect.py`, `data/ingest.py`. Heartbeat and gap monitor not verified. |
| FII/DII flows, filings | Partly | `data/market_inputs.py`. No live feed (owner decision). |
| Global cues (SGX/GIFT Nifty, USDINR, crude) | Not built | Diagnostic scripts only |
| Bulk and block deals | Not built | No code |
| Index-futures hedge overlay | Not built (optional) | No code |
| Live path (`run_live`) | Blocked, not built | See phase 5 |

## 2. Phase status

- **Phase 1, paper bridge: done this session.** Conviction tiers in sizing and the event-blackout feed. 23 new tests in `src/quant/tests/test_phase1_bridge.py`.
- **Phase 2, safety and recovery: mostly present.** Open item: a full kill-restart test on the paper runner (X-7).
- **Phase 3, post-trade and monitoring: modules present.** Not run end to end here. Quality not audited.
- **Phase 4, missing stages: mostly present.** Still to build: global cues, bulk/block deals, hedge overlay. These need data-source decisions first. Beta enforcement needs checking.
- **Phase 5, live path: BLOCKED. Not built.** Owner decisions needed first:
  - live risk limits (null in config)
  - SEBI checklist scope
  - a licensed live data feed
  - `max_symbols_per_portfolio` (until set, the paper gate blocks every order, by design)

## 3. Decisions taken in this session (owner can change)

1. Tier multiplier can raise a size only up to every hard cap (per-order quantity, per-order notional, open-exposure room). Tiers stay disabled. The tier values are unvalidated placeholders.
2. No event calendar loaded means the blackout is NOT checked, and the audit log says so. The alternative is to block all entries. Owner to decide.
3. Blackout window defaults to 1 day before and 1 day after each event (`--blackout-days`).
4. `trade_day` defaults to today's date in IST. An entry with a blackout and no `trade_day` is blocked (fail closed).
5. `main` now contains `master` through a normal merge commit. Nothing was force-pushed.

## 4. Verification

- New tests: `src/quant/tests/test_phase1_bridge.py`, 23 tests, all pass.
- Full quant suite `src/quant/tests`: 459 passed, 13 skipped, 1 xfailed.
- `tests/unit` plus `tests/parity`: 1861 passed, 3 skipped, 1 failed. The failure is `test_upgrade_pin::test_repo_pin_matches_vendored_copy`. It also fails on `master`, so it predates this work.
- Not run here: anything needing `httpx` or `openalgo`. The sandbox cannot install them. Those tests need a CI run.

## 5. Next session, two priorities

1. Owner: set `max_symbols_per_portfolio`, decide the no-calendar policy (decision 2), and name the event-calendar source.
2. Add an end-to-end kill-restart test for the paper runner (X-7), then check beta enforcement in the gate.

## 6. Work log

- 2026-11-09: Merged `master` into `main`. Corrected the inventory. Phase 1 code and tests. Plan written after the first code change, which breaks the rule to plan first. Recorded here for transparency.
- 2026-11-09 (later): Live run design written in `docs/LIVE_RUN_DESIGN.md`. Holiday calendar verified against the NSE circular list (16 equity holidays, files agree). Corrected: live limits are not null (decision 0016), only `max_symbols_per_portfolio` is unset. Live still not enabled.
