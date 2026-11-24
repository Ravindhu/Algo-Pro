# Implementation plan: retire the AlphaEngine scoring stage (2026-11-24)

**Owner decision (2026-11-24):** keep the quant strategy only. Retire the
`AlphaEngine` scoring class, move it to `Temp/Stag/`, and feed POV from the
strategy signal instead.

## 1. What is being retired

- `src/quant/engines/alpha/alpha_engine.py` — the `AlphaEngine` class
  (fundamental + simple price score, pipeline stage `alpha`).
- The `alpha` stage in `src/quant/engines/core/pipeline.py`.

## 2. What is NOT touched (important)

The strategy itself lives in the same folder. These stay where they are:

- `src/quant/engines/alpha/signals/` — strategy registry and base class.
- `src/quant/engines/alpha/Statergy/` — `ocl_keylevel`, patterns, conformation.
- `src/quant/engines/alpha/indicators/`, `src/quant/engines/alpha/confirm/`.

They are imported by `core/runner.py` (live path), `core/screener_bridge.py`
and `optimizer/*`. Moving them would break the live strategy.

## 3. Why this is low risk for live

- `scripts/run/run_live.py` is retired. Live uses `runner.run_with_config`,
  which calls the strategy signals directly. `AlphaEngine` is not on that path.
- The retired stage is only in the macro pipeline (dry-run / analysis) and
  its POV symbol choice.

## 4. Behaviour changes (must be reviewed)

| Area | Before | After |
|---|---|---|
| Pipeline stages | `alpha` runs and ranks symbols | `alpha` stage gone |
| POV symbol/action | from `alpha.selected` / `alpha.signals` | from `context["strategy"]["intent"]` supplied by the caller; else fail-closed (no order) |
| Portfolio gate | needs `alpha.data.selected` | needs `strategy.intent` (no strategy intent = portfolio halted, fail-closed) |
| Report | `alpha_selected` | `strategy_intent` |

Default choice made here (conservative): no strategy intent means no order and
no portfolio selection. The pipeline never picks a symbol by itself.

## 5. Steps

1. Baseline tests on master: **1518 passed, 1 xfailed** (2026-11-24).
2. Move `alpha_engine.py` to `Temp/Stag/alpha_engine/alpha_engine.py`
   (git move, history kept).
3. Remove the `AlphaEngine` export from `engines/alpha/__init__.py`
   (keep `signals`).
4. `core/pipeline.py`: remove the import, the engine instance and the
   `alpha` sort entry; report `strategy_intent` instead of `alpha_selected`.
5. `core/stage_gate.py`: change the portfolio requirement path to
   `strategy.intent`.
6. `pov/pov_engine.py`: read symbol/action/quantity from
   `context["strategy"]["intent"]`; keep the explicit `symbol` override;
   keep fail-closed when neither is present.
7. Tests: add tests for (a) pipeline has no `alpha` stage, (b) POV with no
   intent is skipped, (c) POV uses a supplied strategy intent, (d) portfolio
   halts without intent.
8. Run full quant test suite; must match baseline plus new tests.
9. Commit on branch `plan/retire-alpha-engine`, open a **draft** PR. Do not
   merge without owner review.

## 6. Not in this change (follow-up)

- `macro_master.json` `alpha` block (ignored once the stage is gone).
- `models/alpha_engine.py` (a separate advice wrap, not the scoring class).
- Renaming `alpha/` to `strategy/` (large import change; separate PR).
- Agent desk `alpha` in `core/agent_team.py` and `core/stage_skills.py`.

## 7. Rollback

Revert the commit, or move `Temp/Stag/alpha_engine/alpha_engine.py` back to
`src/quant/engines/alpha/alpha_engine.py`.

## 8. Follow-up status (2026-11-24, implemented on the same branch)

- Done: `models/alpha_engine.py` parked in `Temp/Stag/alpha_engine/` (its 3 tests removed).
- Done: `alpha` block removed from `engines/input/macro_master.json`.
- Done: `alpha` agent skill and "Signal desk" removed from `core/stage_skills.py`
  and `core/agent_team.py`; the portfolio skill now needs `strategy.intent`.
- Done: flow-diagram block "Alpha engine" now maps to the live strategy
  (`alpha/signals/base.py`), so `test_layout` passes.
- Open (separate PR, per section 6): rename `alpha/` to `strategy/`.
- Test result: 1521 passed, 1 xfailed (baseline 1518 + 6 new − 3 removed wrapper tests).
