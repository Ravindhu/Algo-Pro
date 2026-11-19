# Implementation plan: cycle flow for all five strategies (2026-11-19)

Flow stage: Strategy, Strategy bot cycle, Risk gate (exits)
Source: `docs/STRATEGY_RULES_2026-11-19.md` section 11.
Status: PLAN. No code yet. Waiting for owner answer 11.8 before phase 2.

## Phase 1: cycle state machine (pure logic, no orders)
- New module `src/quant/strategies/cycle.py`.
- State per (symbol, strategy): stage, MTF level and low, HTF sweep done, HTF OCL low, HTF invalidation price, list of entries (price, level kind), average entry, fixed target.
- Pure function: given the state and the newest closed bars for MTF, HTF and LTF, return the new state and events (ENTRY, STOP_OUT, HTF_INVALID, MTF_INVALID, NONE).
- Trigger freshness: the entry must be on the newest closed LTF bar.
- Tests: full chain, aggressive then conservative entry, re-entry after stop-out, HTF invalidation, MTF invalidation, average entry and target arithmetic, no HTF re-sweep.

## Phase 2: bot wiring (blocked by owner answer 11.8)
- Bot state is kept per (symbol, strategy) across cycles, in the audit trail.
- Exits use an absolute stop (HTF OCL low) and a fixed target (average entry + 3R), not entry minus ATR distance. The exit manager currently works from entry and distance only, so it needs an absolute-price path.
- Multiple entries in one cycle: each entry is a child position; the position's stop and target follow the average.
- Risk gate: every entry still goes through the gate. Exits go through the same gate.

## Phase 3: paper run and checks
- Paper run on closed bars for the five strategies.
- Audit trail check for each event (entry, stop-out, invalidation).
- Live stays off until owner sign-off.

## Open items (owner)
- 11.8: does a close below the LTF OCL low also close the trade early, with the hard stop at the HTF OCL low?
- 11.9: target fixed at each entry, or only at the first entry?
