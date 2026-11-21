# 0038 — Peg verification on high-volume bid levels (2026-11-21)

## Context
The peg path placed a bounded limit at the bid. The owner asked that the peg conform to the high-volume bid level and only be placed after the level is verified on live data.

## Decision
- Rank the top `peg_depth_levels` (default 20) bid levels by resting quantity, largest first.
- For each of the top `peg_max_levels` (default 3) candidates, run `BidVerifier`. The first level that passes is used.
- Order price = level + `peg_offset_ticks` (default 2) ticks, never at or above the best ask.
- Verification (fail-closed, no verdict means no peg child):
  1. Real absorption: traded volume at the level / initial bid qty >= `peg_absorb_ratio` (0.5).
  2. Low pull ratio: pulled qty / initial bid qty <= `peg_max_pull_ratio` (0.3). Pulled = drop in resting qty not explained by trades.
  3. Dwell / bounce: price touched the level and recovered, and held for `peg_dwell_seconds` (10).
- If no candidate verifies, the parent waits for the next cycle or poll. It does not place a peg child.
- Samples come from the existing 15-second `vwap_tick`, via read-only depth and quote calls. OpenAlgoClient remains the only order caller.

## Consequences
- Peg orders are placed less often and only on levels with observed absorption.
- Config keys `peg_*` are validated in `BotConfig`. Committed config remains paper with live disabled.
- The default thresholds are provisional and may be tuned after paper observation.
