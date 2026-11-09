# Implementation plan: POV engine port (decouple stage 6.5), phases 1-3

Date: 2026-11-09. Owner request: "Wrap 1-3" (POV engine into src/quant).
Scope: `src/quant/execution/` and related modules. No Blue_engine edits. Paper only.
Live stays blocked (Phase 3 gate not met; the data source is not chosen).

## Facts checked in the code

- Blue POV (`Blue_engine/pov/`): about 2,650 lines. Slicer `pov.py` (928), guards `guards.py` (782),
  PEG `peg.py` (249), liquidity slicer `liquidity_pov.py` (215), engine `pov_engine.py` (367).
- The slicer and engine call the OpenAlgo client directly and use module-level state
  (`_LAST_ORDER_AT`, `_LAST_CANCEL_AT`). Quant must not copy that pattern.
- Blue core imports not yet in quant: `core/mode`, `core/quote_health`, `core/reverse_exit`,
  `core/symbol_meta`, `core/time_utils`, `core/volume_cache`, `core/band`, `core/benchmark`, `core/lot`.
- Quant already has: `execution/pov_executor.py` (gate -> queue -> sender skeleton, Phase 3 skeleton),
  `execution/router.py` (`pov_child_qty`), `monitor/throttle.py`, `risk/liquidity.py`,
  `risk/price_band.py`, `core/auction_windows.py`, `risk/squareoff.py`.
- Guards need live quotes and market depth. No licensed live data source is chosen (`config/data_sources.yaml`),
  so guard tests use fixtures.

## Phases

- **Phase A (this doc):** inventory. Done.
- **Phase B, pure sizing and guards:** child-size maths (`band.compute_target_pov_rate`, slice caps,
  lot rounding) and the guards that need only quant modules (circuit, auction window, auction price,
  depth slice cap, depth pricing on a supplied top-of-book snapshot). Parity tests against Blue
  (`tests/parity/`), hand-checked tests in `src/quant/tests/`.
- **Phase C, PEG engine and liquidity slicer:** `peg.py` and `liquidity_pov.py` ported on top of Phase B.
- **Phase D, engine loop (paper only):** `pov_engine.py` timing, stale-cancel and reconciliation, wired
  to the existing `PovExecutor`, with the paper broker only. The analyze bridge stays behind its guard.
  No live path.
- **Phase E, verify and docs:** run new tests, parity tests and the unit suite. Update DECOUPLING_PLAN
  stage 6.5 and the flow lines.

## Rules applied

- No module-level mutable state in the port. Pass state in objects.
- No order call except through `PovExecutor` -> gate -> queue -> client guard.
- Fail closed: missing quote or depth data means refuse the slice (the guard returns a block).

## Work log

- 2026-11-09: Phase A done. Branch `feat/pov-engine-port`. Phases B-E not started.
- 2026-11-09: Phase B done. Local `pov_band.py`, `pov_guards.py`, `pov_sizing.py`. Parity: 86 cases
  (`tests/parity/test_pov_phase_b_parity.py`). Hand-checked: `src/quant/tests/test_pov_phase_b.py`.
  Known issue for owner: Blue `evaluate_guards` is rightmost-wins, so DepthPricingGuard overrides
  AuctionPriceGuard, which contradicts the module comments. Ported unchanged for parity.
- 2026-11-09: Phase C done (partial). Local `pov_peg.py` (PEG engine) and `pov_liquidity.py`
  (core/liquidity.py: tiers, child caps, block scaling). Parity: 99 cases
  (`tests/parity/test_pov_phase_c_parity.py`). Hand-checked: `src/quant/tests/test_pov_phase_c.py`.
  NOT ported yet: the liquidity POV slicer class. It moves to Phase D with the engine loop.
- Phase D (engine loop, paper only) is NOT started. It needs the owner's data-source decision.
