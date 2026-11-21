# 0040 — PegSlicer: the 15 s peg model split from the 5 min POV model (2026-11-21)

## Context
The 5 min cycle sizes parents (POV). The 15 s peg work sat inside the bot's cycle code and shared its state. That made the two models hard to read apart.

## Decision
- `execution/peg_slicer.py` holds the 15 s model: `PegSlicer` (price search, verifier call, peg submit with bounded reprice, tick, settle, report) and `PegAllotment` (one parent's allotment for the cycle).
- `StrategyBot` keeps the 5 min POV model. It arms each parent with `PegSlicer.arm(...)` at the cycle, and calls `peg_slicer_tick` on the 15 s poll.
- Renames: `_peg_price_now` -> `PegSlicer.price_now`; `_submit_peg` -> `PegSlicer.submit`; `_arm_peg` -> `PegSlicer.arm`; `_settle_peg` -> `PegSlicer.settle`; `_drain_peg_report` -> `PegSlicer.drain_report`; `peg_tick` -> `StrategyBot.peg_slicer_tick`; parent fields `peg_waiting/peg_cap/cycle_budget/cycle_spent` -> `parent.peg` (PegAllotment).
- Behaviour unchanged. The allotment is transient and is not saved in the restart document.

## Consequences
- The 15 s model can be reviewed and tested on its own. Its tests use a PegSlicer directly.
