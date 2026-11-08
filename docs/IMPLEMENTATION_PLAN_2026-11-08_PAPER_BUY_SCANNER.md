# Implementation plan: paper-only BUY scanner (2026-11-08)

Status: IN PROGRESS. Branch: `feat/paper-buy-scanner`. Owner: Ravindhu.
Scope set by owner (2026-11-08): buy side only, paper trading only, commit to branch.

## Goal

Every 1 minute, check all screened NSE symbols against the BUY strategies. When a
symbol matches, send the signal through the risk gate and into the POV slicer
(`quant.execution.pov_executor.PovExecutor`). In this phase the sender is a PAPER
sender: it records the child orders and never calls a broker.

## What is ported from Algo-Lab (source: Ravindhu/Algo-Lab, read-only)

| Algo-Lab file | Algo-Pro file | Notes |
|---|---|---|
| `framework/strategies/indicators/hm.py`, `ocl.py` (atr, hm) | `src/quant/strategies/indicators.py` | Pure math, unchanged. |
| `framework/protect.py` | `src/quant/strategies/protect.py` | Forex pip cost removed. Cost is a percent of price. |
| `framework/strategies/multi.py`, `base.py` | `src/quant/strategies/base.py` | No ForexSymbol. Symbol is a plain NSE name. |
| `framework/strategies/ocl.py` | `src/quant/strategies/ocl.py` | BUY only. Forex pip buffer removed. |
| `framework/strategies/ocl_sell_fail.py` | `src/quant/strategies/ocl_sell_fail.py` | TOP and BOTTOM. Handoff store is passed in, not read from a file. |
| `framework/strategies/engulfing_strategy.py` (functions only) | `src/quant/strategies/engulfing.py` | `engulfing()`, `fib_entry_level()`, `body_ratio()`. |
| `framework/strategies/engulfing_ocl.py` | `src/quant/strategies/engulfing_ocl.py` | BUY side only. |

Not ported (out of scope for this phase):
- SELL side (`ocl_inverse.py`). Owner: buy side is enough for equity.
- MT5 adapter, forex symbol wrapper, `ops/state.py` (replaced by the Algo-Pro state layer later).
- DV-ATR trail. Algo-Lab does not port it either. The paper config sets `dvs_enabled: false`.
- The legacy `Engulfing` class (`engulfing_strategy.py`). Only the engulfing HTF setup is used.

## Strategies in this phase (BUY only)

1. `ocl` (normal OCL keylevel)
2. `ocl_sell_fail_top` (failed-short core, tier 1 and tier 2 levels)
3. `ocl_sell_fail_bottom` (single level at lowest swing; runs only after TOP exhausts the same core)
4. `engulfing_ocl` (bullish engulfing with fib entry, OCL LTF entry model)

Open question: the owner asked for "5 strategies". Only these 4 BUY blueprints were found
in Algo-Lab. The fifth one is not identified yet. Waiting for owner answer.

## Build order (each step: tests pass, doc updated, commit, push)

- S1 (this commit): plan doc, ported helpers, the 4 BUY strategies, parity tests against Algo-Lab
  on synthetic data, paper strategy config.
- S2: scanner loop. 1-minute cycle, NSE hours only (09:15 to 15:30 IST, holiday calendar), reads
  closed bars, runs the registry for each screened symbol, persists signal keys so a restart
  cannot trade the same bar twice.
- S3: wire signal to risk gate and `PovExecutor` with a paper sender. Audit log for every signal,
  gate result and child order.
- S4: tests for the whole path with fake bars; a run in paper mode on recorded data.

## Safety rules kept

- Only `openalgo_client.py` may place orders. The paper sender never calls it.
- Strategy code never sets size or changes risk limits.
- signal_frames() is pure: no orders, no file writes, only closed bars (last row dropped).
- Paper only. `live_enabled: false` in the config. Risk limits stay as they are.
- NSE holidays and the Muhurat session (2026-11-08, today) are not yet modelled. The scanner will
  skip cycles on closed days. Owner decision on today's Muhurat session still open.

## Decisions taken without a reply (owner can change)

- Paper only: yes (owner, 2026-11-08).
- Buy side only: yes (owner, 2026-11-08).
- Commit to branch `feat/paper-buy-scanner`, not `main`.
- Fixed quantity per signal for paper: 1 share (until the sizing engine is wired).

## S1 result (2026-11-08)

Tests (run locally, `PYTHONPATH=src pytest tests/unit tests/parity`):
- New: `tests/unit/test_buy_strategies_paper.py`, 27 hand-checked tests, all pass.
- Whole suite: 27 failed, 1921 passed. The 27 failures are the same HRP tests that already fail on
  `main` (baseline 27 failed, 1894 passed). No new failures.

Parity with the Algo-Lab originals (random walks, scratchpad script, not in the repo):
- OCL (C3 only), engulfing OCL: exact match on every step tested.
- Sell-fail TOP: exact match on short history (<= 30 HTF bars). 4800 of 4800 steps match for each
  variant tested. Sample is small: few signals fire on random data, so this is weak evidence.
- Sell-fail TOP with long history: 5 mismatches in 4800 steps. Cause: a bug in Algo-Lab. The
  pattern key uses `htf.index[core_start]` on the full history, but `core_start` is relative to the
  last `max_window` bars. The wrong bar's key misses the "used" list, so a finished core can be
  re-armed. The port uses the correct offset. Regression test: `test_sell_fail_key_points_at_core_start_even_with_long_history`.

Deviations from Algo-Lab (all deliberate):
- Forex pip buffer and pip cost removed. Prices are rupees.
- Sell-side not ported. Only BUY is registered.
- Sell-fail handoff is an in-memory store passed in by the caller. File persistence comes in S2.
- Signal target is a distance (`tp_dist`), same as Algo-Lab. The scanner turns it into a price.

Open items found in this step (owner decision needed):
1. `config/strategies/ocl_keylevel.yaml` says `aggressive_level_source: "C3_CLOSE"` and
   `conservative_level_source: "C3_OPEN"`. The Algo-Lab port (and this port) uses C2 open as the
   aggressive level after a C3 break, and C3 close only after a C4 confirmation. The YAML needs
   to be checked against this before any YAML-to-params loader is written. The loader is not in S1.
2. `dvs_enabled: true` in that YAML. DV-ATR is not ported. The paper config must set it to false
   (same as Algo-Lab, which also does not port it).
3. The owner asked for "5 strategies". Only 4 BUY blueprints exist in Algo-Lab
   (OCL, sell-fail TOP, sell-fail BOTTOM, engulfing). The fifth is not identified yet.

Status of the layout test: all `src/quant/strategies/*.py` files carry `Status: PHASE 2 IMPLEMENTED`
(paper only). This is required by `tests/unit/test_layout.py`.

Next (S2): scanner loop, NSE session and holiday checks, persisted signal keys, and a paper run
over recorded bars.
