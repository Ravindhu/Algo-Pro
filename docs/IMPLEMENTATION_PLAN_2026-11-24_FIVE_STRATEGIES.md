# Implementation plan: keep five strategies, keep bot, paper and live (2026-11-24)

## Owner request
1. Move the old runner to Temp/Stag.
2. Keep the bot, paper runner and live runner.
3. Keep only these five strategies; move every other strategy to Temp/Stag:
   - Engulfing (`engulfing`)
   - OCL (`ocl_seq`)
   - Sell fail OCL top (`ocl_sell_fail_top_seq`)
   - Sell fail OCL bottom (`ocl_sell_fail_bottom_seq`)
   - Sell fail engulfing (`engulfing_fail`)

These five are exactly the `strategies:` list in `config/bot.yaml`, and they are
defined in `src/quant/strategies/owner_patterns.py` (`SEQUENCE_KINDS`).

## Static import closure of the kept code (computed 2026-11-24)
Entry points: `execution/strategy_bot.py`, `strategies/owner_patterns.py`,
`strategies/cycle.py`, `strategies/risk_ladder.py`, `strategies/engulf_sequence_strategy.py`.
Result: 46 modules. Strategy-related ones: `strategies/owner_patterns`, `cycle`,
`risk_ladder`, `risk_tree`, `engulf_sequence_strategy`, `signals/base`,
`signals/risk_adapter`, `indicators/indicators`.

## Candidates to move (not in the closure)
| Item | Location | Note |
|---|---|---|
| Legacy Engulfing class | `src/quant/strategies/engulfing/` | Only used by `run_strategy_bot.py` as an unused import (line 43). Remove that import. |
| Sample strategy | `src/quant/strategies/signals/sample_strategy.py`, `confirm_state.py` | Not used by the bot. |
| Signal strategies | `src/quant/engines/alpha/signals/` (keylevel, base, _pattern, _template) | Old runner and research scripts only. |
| Statergy + confirm | `src/quant/engines/alpha/Statergy/`, `alpha/confirm/` | Legacy OCL keylevel. Not the kept OCL (`ocl_seq`). |
| Old runner | `src/quant/engines/core/runner.py` | Owner item 1. |
| Old-runner scripts | `scripts/run/run_pov.py`, `run_real.py`, `engine_entry.py` | Import the old runner or the signals. |
| Research scripts | `scripts/diagnostics/deep_dive.py`, `scripts/research/e2e_replay.py`, `make_real_levels.py`, `scripts/train/backtest_ocl_confirm.py`, `scripts/verify/smoke_test.py` | Import the moved signals. |
| Engulfing indicator | `src/quant/indicators/engulfing.py` | Used only by the legacy Engulfing class and its test. |

## Open decisions (need owner answer before code changes)
A. **Sector engine** imports `alpha/indicators/hilega_milega.py` (`sectors/sector_hm_score.py`).
   Options: (1) keep `alpha/indicators/` in place and move only signals/Statergy/confirm;
   (2) move the indicator to `src/quant/indicators/` and update the one import (recommended).
B. **Optimizer** (`engines/optimizer/spaces.py`, `data_cache.py`) imports alpha signals and
   names SELL_FAILURE modes. Options: (1) move the optimizer too; (2) remove the alpha import
   and keep the optimizer on the five kept strategies (recommended, research only).

## Steps after decisions
1. Move the candidates above to `Temp/Stag/<area>/` with `git mv`, keeping paths.
2. Remove the dead Engulfing import from `run_strategy_bot.py`.
3. Update the flow-diagram mapping (`test_layout`): the "Alpha engine" block must map to a kept module (`strategies/owner_patterns.py` docstring).
4. Move or fix tests that import moved code; no test deleted without a replacement or a note.
5. Run the full quant suite. Baseline on this branch: 1521 passed, 1 xfailed.
6. Commit with attribution trailers; push; verify remote head.

## Rollback
Move files back from `Temp/Stag/` to their original paths (git revert of the commit).

## 9. Result (implemented on the same branch)

- Kept: `execution/strategy_bot.py`, `scripts/run/run_strategy_bot.py` (paper),
  `scripts/run/run_strategy_bot_live.py` (live), and the five sequence strategies in
  `strategies/owner_patterns.py` (with cycle, risk_ladder, risk_tree, engulf_sequence_strategy,
  signals/base, signals/risk_adapter, indicators/indicators).
- Parked in `Temp/Stag/`: old runner (`engines/core/runner.py`, `screener_bridge.py`,
  run_pov, run_real, engine_entry, run_supervised, run_screener_bridge, the old shims);
  `engines/alpha/signals`, `Statergy`, `confirm`, `registry.py`; `strategies/engulfing`,
  `sample_strategy.py`, `confirm_state.py`; `indicators/engulfing.py`; research scripts;
  tests that exercised parked code (engulfing, signals base/engulfing/roc_state, engine_entry).
- Moved and kept live: `alpha/indicators/hilega_milega.py` -> `src/quant/indicators/` (sector engine).
- Dead import removed: `run_strategy_bot.py` (legacy Engulfing).
- Optimizer: parked signal listing now returns an empty list (broad except); no change to its logic.
- Doc: wrapped-module count **46** (verified 2026-11-24); retired wrapper row marked.
- Test result: quant suite and tests/ folder: 2372 passed, 2 skipped, 1 xfailed.
  Skipped: one Blue-alpha-registry test in test_phase5_wraps (registry parked). Coverage lost: the
  parked tests above; the live-gate tests for the kept live runner are still in place.

## 10. Folder move (2026-11-24, same branch)

- `src/quant/strategies/` moved to `src/quant/engines/alpha/strategies/` (the five live strategies).
- `src/quant/indicators/` moved to `src/quant/engines/alpha/indicators/`.
- 25 files rewritten to the new import paths; sector engine import updated.
- Doc: module count 43 (verified 2026-11-24). Tests: 2372 passed, 2 skipped, 1 xfailed.
