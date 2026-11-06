# Implementation plan: staged decoupling of src/quant from Blue_engine

Status (2026-11-06): stages 1-2 DONE; stage 3 PARTIAL (items 6, 7, 9-12 local and parity-tested; item 8 HRP still via Blue bridge); stages 4, 5, 6 NOT STARTED (owner review / decision needed).
Started: 2026-11-05. Owner decision: "Staged decoupling" (docs/decisions/0011-decouple-from-blue-engine.md).

## 1. Goal
`src/quant/` runs without importing `Blue_engine/`. Then `Blue_engine/` can move to `Temp/archive/` (git keeps history).
Every replacement must match Blue's results. Nothing trading-related changes behaviour silently.

## 2. Scope
In scope: the 14 call sites in section 4 (13 Blue functions + 1 env loader).
Out of scope now: the optimizer, screener, economics, agent pipeline, POV engine. They are Blue-only features. Their fate is an owner decision (section 8, D2).

## 3. Constraints that shape the plan
- The cloud sandbox cannot install `openalgo`. 6 of 13 Blue functions only load on the owner machine.
- Fix: **golden fixtures**. The owner runs one script on the owner machine. It runs each Blue function on fixed inputs and saves the outputs to JSON. The sandbox then tests the local code against those JSON files, with no Blue import needed.
- Hand-checked tests run everywhere. Parity tests run wherever Blue loads. Golden tests run everywhere after fixtures exist.

## 4. Inventory
| # | quant call site | Blue function | Sandbox loads? | Stage | Method |
|---|---|---|---|---|---|
| 1 | research/validation.py | ml.evaluation.metrics.sharpe | yes | 1 | local + parity (done) |
| 2 | monitor/live_monitor.py | ml.evaluation.metrics.max_drawdown | yes | 1 | local + parity (done) |
| 3 | research/costs.py | backtest.costs.CostModel | yes | 2 | local + parity |
| 4 | research/walk_forward.py | ml.evaluation.splits.walk_forward | yes | 2 | local + parity |
| 5 | models/factors.py | ml.features.engineering.momentum | yes | 2 | local + parity |
| 6 | reporting/dashboard.py | backtest.metrics.profit_factor | no | 3 | DONE: local + parity (`test_stage3_parity.py`) |
| 7 | portfolio/covariance.py | portfolio.risk.covariance.ledoit_wolf_cov | no | 3 | DONE: local + parity (rel 1e-9, abs floor 1e-8 x max entry) |
| 8 | portfolio/optimizer.py | portfolio.optim.hrp.hrp_weights | no | 3 | PENDING: still via blue_bridge; local + golden (see note: decision 0009) |
| 9 | portfolio/portfolio_risk.py | portfolio.risk.tail.cvar | no | 3 | DONE: local + parity (`test_stage3_parity.py`) |
| 10 | portfolio/rebalance.py | portfolio.optim.cost_aware.should_rebalance | no | 3 | DONE: local + parity (`test_stage3_parity.py`) |
| 11 | portfolio/sizing.py | portfolio.sizing.kelly.kelly_leverage | no | 3 | DONE: local + parity (`test_stage3_parity.py`) |
| 12 | portfolio/sizing.py | portfolio.sizing.vol_target.vol_target_scale | no | 3 | DONE: local + parity (`test_stage3_parity.py`) |
| 13 | execution/openalgo_client.py | core.client.load_env_file | yes (python-dotenv) | 4 | DONE: `core/env.py`, tests `test_dotenv_loading.py` (decision 0019); owner sign-off pending |
| 14 | risk/money_checks.py (was blue_risk_gate.py) | core.risk_gate.RiskGate (money checks) | yes (pure, no stubs) | 4 | DONE: ported + parity (`test_money_checks_parity.py`); owner sign-off pending (D3) |

## 5. Stages

### Stage 1 — metrics (items 1-2). Status: DONE (local, not yet committed)
- Added `src/quant/research/perf_metrics.py` (sharpe, max_drawdown; same formulas and edge cases as Blue).
- Switched validation.py and live_monitor.py to it.
- Tests: `test_perf_metrics.py` (hand-checked, 6 tests), `test_perf_metrics_parity.py` (40 parity cases vs live Blue; passes here). `test_phase0_wrappers.py::test_sharpe_delegates_to_blue` replaced by `test_sharpe_matches_blue_result` (the old test asserted the Blue call happens, which is no longer the design).
- Exit check: all 51 new/affected tests pass here; no new unit failures vs baseline (to be re-checked before commit).

### Stage 2 — loadable-here items (3-5). Status: DONE 2026-11-06 (parity passes here)
- Done: 3 CostModel is local (`research/costs.py`). Parity grid: `tests/parity/test_costs_parity.py` (24 cases, rel 1e-9).
- Done: 4 walk-forward is local (`research/walk_forward.py`, `purged_walk_forward`). Parity: `tests/parity/test_walk_forward_parity.py` (8 cases, fold indices equal). Embargo is 0 in both; purge gap test kept in `test_phase0_wrappers.py`.
- Done: 5 momentum is local (`models/factors.py::momentum`). Parity: `tests/parity/test_momentum_parity.py` (random series + NaN/zero-base cases).
- Blue is loaded only when importable; otherwise the parity tests load Blue's source file in isolation with stubbed imports, so they run in the sandbox.
- Exit check: `pytest tests/parity` = 38 passed; `pytest tests/unit` = 44 failed (all `No module named 'httpx'`, was 70 before this stage), 350 passed.
- Original plan (kept for reference):
- 3 CostModel: port `backtest/costs.py` rules into `research/costs.py` (currently a wrapper). Parity test on a grid of trades (BUY/SELL, delivery/intraday, value bands). Hand-checked STT/stamp/GST cases already exist (X-5); keep them.
- 4 walk_forward splitter: port `ml/evaluation/splits.py::walk_forward` (purged, embargoed folds). Parity on fold boundaries and purge sizes. Look-ahead rule must hold (test_point_in_time tests stay green).
- 5 momentum: port `ml/features/engineering.py::momentum`. Parity on random series + NaN handling.
- Exit check: each item has local code, hand-checked test, parity test (runs here), wrapper removed from call site.

### Stage 3 — openalgo-dependent portfolio/risk items (6-12). Status: PARTIAL (2026-11-06)
- Done here: items 6, 7, 9, 10, 11, 12. Local code, checked against Blue by isolated-loader parity (runs in sandbox; owner goldens not needed for these).
- Deviation from the plan: the sandbox could load these Blue leaves via the stub loader, so parity replaced golden fixtures. Golden export script is still needed for item 8 and for owner-machine regression.
- Pending: item 8 HRP (optimizer still calls blue_bridge). Owner goldens (`scripts/ops/export_blue_goldens.py`) not written.
- Note: the earlier Kelly/vol-target floor quirk (Blue returns 1.0 for 0 < vol <= 1e-6) is kept on purpose; owner to review.
- Owner-machine step (one script, `scripts/ops/export_blue_goldens.py`, to write): runs each Blue function on fixed inputs (seeded), writes `tests/fixtures/blue_goldens/<name>.json`. Commit fixtures.
- Sandbox step: local implementation + golden test reads the JSON. Tolerance: rel 1e-9 unless the function is an optimiser (then exact weights within 1e-6).
- Item 8 note: our HRP already matches the published algorithm (decision 0009, owner still to choose). Stage 3 must not change HRP output without owner choice.
- Exit check: 7 goldens committed; local code passes them; call sites switched.

### Stage 4 — risk-critical items (13-14). Status: CODE DONE (2026-11-06). Owner sign-off pending.
- 13 `load_env_file`: DONE. `src/quant/core/env.py` (decision 0019). `api_key_from_env()` no longer calls Blue. Tests: missing file, comments, quotes, export prefix, no overwrite of existing variables.
- 14 RiskGate money checks: DONE (code + parity), decision 0018. Ported to `src/quant/risk/money_checks.py` with no Blue import. The old Blue-backed wrapper moved to `Temp/Staging Workspace/`. Kept: fail-closed on unset limits (pretrade, loss breaker), exits never blocked by entry limits, duplicate-ID block, daily loss and exposure caps. Parity test: `tests/parity/test_money_checks_parity.py` (34,560 grid cases + edge cases, zero differences against Blue's real RiskGate, exact breach text).
- Exit check: golden/parity diff = 0 (met for item 14). Owner sign-off recorded in docs/decisions/ is STILL PENDING (decision 0018, D3).

### Stage 5 — remove Blue_engine from the quant path. Status: NOT STARTED (blocked by stages 3-4 and stage 6)
Steps:
1. grep: no `blue_bridge` calls and no `blue_engine` imports under `src/quant/` or `tests/unit/` (except the parity tests, which move to a `tests/parity/` folder that the owner runs with Blue present).
2. Installers: remove Blue from `requirements.txt`, `install.bat`, `install_ubuntu.sh` if no other owner feature needs it (see D2).
3. Move the remaining `Blue_engine/` folder to `Temp/archive/<date>/Blue_engine/` (git history keeps it). Only after stage 6 is done.
4. Update AGENTS.md reuse map and layout tree; update SOP and README.
5. Full unit suite must be no worse than the pre-stage baseline.
Exit check: `pytest tests/unit` passes except tests that need owner-only data; quant runs paper mode from a clean checkout with Blue removed.

### Stage 6 — migrate the Blue-only features into src/quant (D2). Status: NOT STARTED (needs owner D2 decision first)
Features: optimizer, screener, economics, agent pipeline, POV engine (see docs/decisions/0012).
- One feature per sub-stage. Order: screener (smallest) -> economics -> optimizer -> POV engine -> agent pipeline (last; advice only).
- Owner decision D2 = move into quant (2026-11-06, "yes move to quant").
- 6.1 screener rules: DONE 2026-11-06. `src/quant/data/screen_rules.py` (rules, three-valued verdicts, sector gate, default two-rule screen, to_number subset). Parity: `tests/parity/test_screen_rules_parity.py` (826 cases). Hand-checked: `tests/unit/test_screen_rules.py` (20). Known difference: local refuses unknown `on_missing` values (stricter). Not ported: `to_number(unit_scale="absolute")`, unused by rules.
- 6.2 screener.in client, annual-report PDF parsing, yfinance fallback, valuations (about 6,800 lines in Blue_engine/screener): PENDING. Network-bound; needs owner data access and a split into sub-steps.
- 6.3 economics, 6.4 optimizer (HRP item 8 included), 6.5 POV, 6.6 agent: PENDING.
- Also pending for each sub-stage: SVG mapping and Flow stage lines.
- Each sub-stage: move code into the owned src/quant package, re-structure to the layout, add tests (move the matching Blue tests), update the Flow stage line and SVG mapping, and keep behaviour (parity or golden where Blue loads).
- Exit check per sub-stage: tests pass (or owner-only data noted), no import of blue_engine from that feature.
- OpenAlgo SDK stays a pinned pip dependency (decision 0012).

## 6. Test strategy (per item)
| Kind | Where it runs | Purpose |
|---|---|---|
| Hand-checked | everywhere | fixed numbers worked out by hand |
| Parity (live Blue) | where Blue loads (sandbox for items 1-5) | exact match to Blue |
| Golden (JSON from Blue) | everywhere, after fixtures exist | match to Blue without openalgo |

## 7. Risk register
| Risk | Effect | Control |
|---|---|---|
| Silent numeric drift | wrong sizing or P&L | parity or golden test per item; tolerance written down |
| Risk gate changes behaviour | unsafe orders | stage 4 owner review; golden diff = 0 |
| Golden fixtures stale | false passes | fixture file records Blue git SHA; script refuses to run on a dirty tree |
| Owner machine not run | stage 3 blocked | stage 2 is independent; stage 3 waits |
| Deleting Blue too early | optimizer/screener break | stage 5 only after D2 decision |

## 8. Owner decisions
- D2 RESOLVED (2026-11-05): keep the optimizer, screener, economics, agent pipeline and POV engine inside src/quant/. See docs/decisions/0012-keep-blue-features-inside-quant.md. New stage 6 below.
- D3: confirm stage 4 review (who signs off RiskGate port).
- D4: owner machine time to run `export_blue_goldens.py` (stage 3).
- D1 (earlier): HRP choice (decision 0009) before stage 3 item 8.

## 9. Work log
- 2026-11-05: inventory and decision 0011. Stage 1 done and pushed (5df227f). D2 resolved: decision 0012, stage 6 added.
- 2026-11-06: stage 4 item 14 (RiskGate money checks) ported to `src/quant/risk/money_checks.py`, parity zero diff, decision 0018. Item 13 and owner sign-off still open.
