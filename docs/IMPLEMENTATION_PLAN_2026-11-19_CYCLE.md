# Implementation plan: cycle flow for all five strategies (2026-11-19)

Flow stage: Strategy, Strategy bot cycle, Risk gate (exits)
Source: `docs/STRATEGY_RULES_2026-11-19.md` section 11.
Status: PHASE 1 DONE (2026-11-19). PHASE 2 DONE in paper mode (2026-11-19): exit rules, next HTF cycle, freshness, bot wiring, restart guard. Backtested target N still open. Live stays off.

## Phase 1: cycle replay (pure logic, no orders) — DONE
- Built as a replay over closed bars (`src/quant/strategies/cycle.py`, `run_cycle`), so no state is stored between cycles.
- Tests: `tests/unit/test_cycle.py` (10 tests): full cycle with trail, aggressive-only target, target pending, HTF invalidation, MTF invalidation, no touch, no sweep, re-entry after a close below the LTF OCL low, and the below-previous-level rule.
- Owner answers used: 11.8 (no LTF exit; a new LTF OCL below the old level adds an entry), 11.9 (trail after a conservative touch; otherwise the backtested target).
- Not done: the target N is not known (needs a backtest). Only the first HTF cycle is replayed. After an HTF invalidation, the wait for a new HTF OCL is phase 2.
- New module `src/quant/strategies/cycle.py`.
- State per (symbol, strategy): stage, MTF level and low, HTF sweep done, HTF OCL low, HTF invalidation price, list of entries (price, level kind), average entry, fixed target.
- Trigger freshness: the entry must be on the newest closed LTF bar.
- Tests: full chain, aggressive then conservative entry, re-entry after stop-out, HTF invalidation, MTF invalidation, average entry and target arithmetic, no HTF re-sweep.

## Phase 2: bot wiring
Done (2026-11-19):
- `exit_manager.Position`: target may be None (no fixed target), `breakeven_enabled` (False for cycle positions), `trail_from_start` (ATR trail from entry). Defaults unchanged, existing tests pass.
- `tests/unit/test_cycle_exit.py` (5 tests).

Still open (the bot's position model is one Position per signal parent, so these need a design step before code):
- Cycle position (rules doc section 12): one position per HTF cycle. Quantity sized once at the first trigger. The POV slicer fills it; later triggers keep the same quantity; an LTF invalidation pauses the POV slicer and keeps the unfilled quantity, which resumes at the next LTF OCL touch. Average = average fill price. One hard stop (HTF OCL low), one exit mode. Today each parent is its own Position.
- Sizing: `risk_adapter.levels_from_signal` works from entry and distances. The cycle needs an absolute stop path. Each added entry changes the average, so the quantity must be re-sized or added on.
- Broker stop (SL-M) sync must use the HTF OCL low, and move when the trail moves.
- Entries become orders only when the touch is on the newest closed LTF bar (`_scan` wiring).
- After an HTF invalidation: wait for a new HTF OCL below the invalidated price, then its key touch (replay only covers the first HTF cycle).
- Restart: cycle state must survive a restart (positions are restored from file today).
- The bot's breakeven and fixed-target defaults must stay off for cycle positions.

- Bot state is kept per (symbol, strategy) across cycles, in the audit trail.
- Exits use an absolute stop (HTF OCL low) and a fixed target (average entry + 3R), not entry minus ATR distance. The exit manager currently works from entry and distance only, so it needs an absolute-price path.
- Multiple entries in one cycle: each entry is a child position; the position's stop and target follow the average.
- Risk gate: every entry still goes through the gate. Exits go through the same gate.

## Phase 3: paper run and checks
- Paper run on closed bars for the five strategies.
- Audit trail check for each event (entry, stop-out, invalidation).
- Live stays off until owner sign-off.

## Open items
- Backtested target N for the aggressive-only exit (owner: needs a backtest).

## Status update (2026-11-19, later)
- DONE: next HTF cycle in the replay (`run_cycles`): after an HTF invalidation, the first new HTF OCL that forms below the invalidated price is used, its key must be touched, and the LTF loop runs again. No new sweep.
- DONE: sweep rule: the NEWEST 3-candle swing low that formed before the MTF touch and is broken after the touch (`first_sweep_after`).
- DONE: HTF OCL choice: the first (oldest) OCL after the sweep.
- DONE: HTF invalidation uses HTF candle closes (rule 12), and it ends the cycle even before an entry.
- DONE: order freshness helper `fresh_entries` (only entries on the newest closed LTF bar may become orders).
- DONE (tests): `tests/unit/test_cycle.py` 14 tests, incl. next HTF cycle.
- DONE: bot wiring in `strategy_bot.py` (`_scan_cycle`, paper mode):
  - One parent per HTF cycle, keyed by `sym|strategy|BUY|cycle|<HTF key bar time>`. The first fresh trigger sizes the quantity through the full risk gate (`_gate_entry`). Later triggers in the same cycle add nothing.
  - Entries count only on the newest closed LTF bar (`fresh_entries`).
  - LTF stop-out (`CycleResult.ltf_paused`) pauses slicing. The next LTF touch resumes it with the same remaining quantity.
  - POV fills average into one Position (existing `_record_fill`). Stop = HTF OCL low, and the broker stop follows `Position.stop`.
  - Cycle positions: breakeven off. Trail from entry only after a conservative touch. Target = average + N x R, or None while N is not set.
  - Earlier HTF cycles that ended: their unfilled quantity is dropped. Open positions stay under their stop.
  - Restart guard: `_cycle_done` is persisted across days and restarts. A cycle that already opened, or still has a position, is never opened again with a second quantity.
  - Tests: `src/quant/tests/test_strategy_bot_cycle.py` (7 tests), `tests/unit/test_cycle_state.py` (4 tests).
- NOT DONE / KNOWN GAPS:
  - `cycle_position.py` (`CyclePosition`) is tested but NOT used by the bot. The bot tracks the same state in `_Parent` and `Position`. Decide later: delete it or use it. Do not run both.
  - The unfilled quantity of a cycle is not restored from disk (only the position is). After a restart the remaining quantity of that cycle is dropped, never refilled. Safe, but it means less fill than the rules intend.
  - The gate's `tp_dist` for cycle entries is a placeholder (3 x risk). Exits never use it.
  - The broker SL-M sync at the HTF OCL low is inherited from the existing stop path and is not separately verified against OpenAlgo.
  - Sell-fail trigger candle, OCL-top 4-candle conservative level (C3 open vs close) and the breakeven default are still owner decisions (see the rules doc).
- NOT DONE: backtested target N. The repo has no historical OHLCV bars (data/ has only fii, sectors and a key file), so N cannot be found here. Needs bar history from the owner's OpenAlgo setup.
