# Implementation plan: cycle flow for all five strategies (2026-11-19)

Flow stage: Strategy, Strategy bot cycle, Risk gate (exits)
Source: `docs/STRATEGY_RULES_2026-11-19.md` section 11.
Status: PHASE 1 DONE (2026-11-19). PHASE 2 PARTLY DONE: exit rules only. Bot wiring still open (see below).

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
- Cycle position: all entries of one HTF cycle form ONE position with an average entry, one hard stop (HTF OCL low), and one exit mode (trail or target). Today each parent is its own Position.
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
