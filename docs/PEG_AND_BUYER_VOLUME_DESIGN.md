# Design note: buyer-initiated VWAP, buyer volume, PEG, band slicing (2026-11-15)

Plain-English design for the four items the owner asked about. Live trading stays OFF.

## 1. Buyer-initiated VWAP
- Definition in the code: a candle counts only when the last traded price is at or above the ask
  (`ltp >= ask`). The VWAP is the price-weighted average over those counted candles only
  (`pov_band.BuyerInitiatedDailyVWAP`).
- Source now used: live quote polls, not historical candles. Each poll reads LTP, ask and
  cumulative day volume (OpenAlgo `quotes`). Volume added since the last poll counts as buyer-initiated
  only when LTP >= ask at that poll. VWAP = sum(LTP x counted volume) / sum(counted volume).
- Built in `src/quant/execution/buyer_vwap.py` (`BuyerVwapBook`), one accumulator per screened symbol,
  reset each IST day. The live runner polls every `vwap_poll_seconds` (default 15) only in band mode.
- Limits: poll-based, not trade by trade. A restart re-baselines, so the day's VWAP is partial after one.
  Intervals with no LTP, ask or volume are not counted.
- Open check: confirm on the analyzer that `quotes` returns `ask` and a cumulative `volume` for NSE stocks.
  Until then, band mode may fall back to fixed POV when no buyer volume is counted.

## 2. Buyer-initiated volume (from buyer-initiated orders)
- Built as an estimate from the same quote polls as item 1 (`BuyerVwapBook.buyer_volume`, `buyer_share`).
- Meaning: shares traded in intervals where LTP >= ask, out of shares traded in intervals whose side could be decided.
- It is not a count of market orders. OpenAlgo's order book only shows this account's own orders, so market-wide order flow is not available.
- Limits are the same as item 1 (poll-based, restart re-baselines the day).
- Slicing (`pov_mode: buyer`): child qty = pov x (last closed bar volume x buyer share of today's volume).
  Only the buyer-side part of the volume is used to size children. With no buyer share yet, it uses the full
  bar volume and logs the fallback. Fixed mode is unchanged.
- Approximation: the day's buyer share is applied to the last bar, not to that bar's own buyer volume.
- Combined (`pov_mode: band_buyer`): band sets the rate (pov), buyer share sets the volume. Child = rate x (last bar volume x buyer share).
  Each part falls back on its own: no VWAP -> fixed pov rate; no buyer share -> full bar volume. Halt zone -> no child that cycle.

## 3. PEG order
- Status: NOT BUILT, by owner decision (2026-11-16). Left out until the owner asks for it.
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
