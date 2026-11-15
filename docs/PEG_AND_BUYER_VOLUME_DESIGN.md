# Design note: buyer-initiated VWAP, buyer volume, PEG, band slicing (2026-11-15)

Plain-English design for the four items the owner asked about. Live trading stays OFF.

## 1. Buyer-initiated VWAP
- Definition in the code: a candle counts only when the last traded price is at or above the ask
  (`ltp >= ask`). The VWAP is the price-weighted average over those counted candles only
  (`pov_band.BuyerInitiatedDailyVWAP`).
- Blocker: the ask at each historical candle, or a trade tape, is needed. OpenAlgo's verified
  endpoints (`quotes`, `depth`, `history`) do not give per-candle ask history. Not built.
- Until a verified source exists, the band uses no VWAP and falls back to fixed POV.

## 2. Buyer-initiated volume (from buyer-initiated orders)
- Needs trade-level data (each trade with its price and whether it hit the ask). Same blocker as item 1.
- Not built. Would need the owner to confirm a tape source first.

## 3. PEG order
- OpenAlgo pricetypes are MARKET, LIMIT, SL and SL-M only. PEG is not supported.
- Emulation would be a LIMIT order that is cancelled and re-placed at a new peg price. That needs
  a repricing loop much faster than the 5-minute bot cycle. Not built. Needs an owner decision on
  whether to build a fast loop.

## 4. Band-based POV slicing (built now, behind a switch)
- Config: `pov_mode: fixed` (default, the old behaviour) or `pov_mode: band`.
- Band uses the existing `pov_band.compute_target_pov_rate` (no new band code).
- Direction, as asked: LTP near the band's ceiling (high of band) gives the LOWER POV; LTP near the
  floor (low of band) gives the HIGHER POV. Above the ceiling = HALT (no child this cycle).
- LTP used by the bot: the last closed bar's close (the bot runs on closed bars).
- VWAP comes from an injected `vwap_provider(symbol)`. Default is none, so band mode falls back to
  the fixed `pov` until a verified buyer VWAP source is supplied. The fallback is logged.
- Passthrough zone uses the fixed `pov` (part_pct is set to pov x 100), so below the floor
  behaviour matches fixed mode.
- Safety: the executor still caps each child at what is left of the parent, and every child still
  goes through the risk gate. Band can only change the size of a child, never the risk limits.
- Rates: `min_pov` and `max_pov` are fractions (0 to 1]; min must not exceed max.

## Owner decisions needed
1. A verified source for ask or trade-tape data (blocks items 1 and 2).
2. Whether to build a fast PEG repricing loop (item 3).
