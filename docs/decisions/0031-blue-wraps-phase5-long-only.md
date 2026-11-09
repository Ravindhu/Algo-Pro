# 0031 - Phase 5 wraps: seven Blue modules wrapped, long-only enforced in code

Date: 2026-11-09
Status: accepted

## Decision

Screener, economic, POV, agent team, optimizer, backtest, feedback and strategy
modules stay in `Blue_engine/` and are wrapped from `src/quant/`. None is re-implemented.

| Blue_engine source | quant wrapper |
|---|---|
| `screener/screener_engine.py` | `data/screener_engine.py` |
| `economics/economic_engine.py` | `data/economic_engine.py` |
| `pov/pov_engine.py` | `execution/pov_engine.py` |
| `core/agent_team.py`, `core/stage_gate.py` | `models/events/agent_team.py` |
| `optimizer/engine.py`, `optimizer/analysis.py` | `research/optimizer.py` |
| `backtest/engine.py` | `research/backtest.py` (`run_symbol_backtest`) |
| `core/auto_tune.py` | `feedback/auto_tune.py` |
| `alpha/registry.py`, `optimizer/strategies.py` | `strategies/__init__.py` |

The local portfolio replay (`research/backtest.run_backtest`) stays: Blue is
single-symbol (decision 0002).

## Hard boundaries added by each wrapper

1. `select_universe` - junk rows dropped, blacklist wins, cap applied last.
2. `risk_multiplier` - clamped to `[0.5, 1.0]`; a stale feed may not lever up;
   an unreadable multiplier returns the floor, not 1.0.
3. `plan_pov` - Blue always runs in forced dry-run with no client in the context,
   so the slicer's submit path is unreachable. It returns a `PovPlan`.
4. `run_shift` - `ready_to_trade` is forced to `None` in the ledger; the stage
   gate's verdict is exposed as data.
5. `run_optimisation` - refused without a `trial_log_path` (an unlogged run is
   not evidence); `visual_workflow` returns the 3-step chart order.
6. `run_symbol_backtest` - Blue's fill convention, fed with the local cost table
   (`costs.blue_cost_model`), so research and optimisation agree on charges.
7. `review_batch` - the OOS policy is recomputed; an `apply` request is refused,
   so no parameter file is ever written from the feedback loop.

## Long-only (owner, 2026-11-09)

This is an investment algo: BUY and ADD only, never short. Enforced at four
gates, because a rule in a document does not protect capital:

- `data/screener_engine.select_universe(long_only=True)` - drops inverse/bear
  products and names containing inverse/bear/short.
- `execution/pov_engine.plan_pov` - a SELL/SHORT plan is `skipped` unless the
  caller passes `exit_leg=True`, which is only ever an exit of an open long.
- `research/backtest.run_symbol_backtest` - `allow_short=True` raises.
- `strategies.guard_intent` - allows BUY/ADD and no action; a SELL is allowed
  only with `exit_leg=True`.

## Consequences

- Two simulators exist and both are long-only. The optimiser scores
  `run_symbol_backtest`'s engine, so an optimised parameter set is reachable by
  the live path.
- A short strategy can still be *researched* by turning the switch off in code,
  but the live wrappers refuse it. If the desk ever wants shorts, this decision
  must be replaced, not bypassed.
- Tests: `src/quant/tests/test_phase5_wraps.py`.
