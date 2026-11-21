# 0039 — Peg work every 15 s inside each 5 min cycle allotment (2026-11-21)

## Context
Peg parents waited for the next 5 min cycle when no high-volume level verified. Verification samples are taken every 15 s, so a level that verified between cycles was not acted on until the next cycle.

## Decision
- Each 5 min cycle sets the parent's allotment: `cycle_budget` = the POV child size for that cycle, before peg caps. A new cycle replaces it. Nothing is added between cycles.
- Between cycles, `peg_tick` runs on the existing 15 s poll. A waiting peg parent places a peg only when the verifier passes a level, and only while `cycle_spent < cycle_budget`. Each peg is still capped at `peg_bid_fraction` of the shares at the level.
- Pegs use the same `_submit_peg` path (bounded reprice, clean cancel). Filled shares count against the allotment. Unfilled pegs do not.
- Fills and unfilled pegs are counted and returned in the next cycle report under `peg_15s`.
- Not applied to cycle (HTF ladder) parents, exits, or non-peg modes.
- Cycle still owns sizing, risk gate and the next allotment. Entries blocked or an open fault stops 15 s pegs.

## Consequences
- Live orders can now go out between cycles, within the cycle's allotment. This needs owner review and paper observation before live use.
