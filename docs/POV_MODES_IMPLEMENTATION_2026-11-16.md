# POV modes: how the implementation works (2026-11-16)

Plain-English guide to how the bot sizes and prices child orders. Live trading stays OFF.
`config/bot.yaml` keeps `mode: paper` and `live_enabled: false`.

Design background: `docs/PEG_AND_BUYER_VOLUME_DESIGN.md`.

## 1. The big picture

Every 5-minute bot run, for each open parent (an entry being sliced):

1. Work out the **rate** (how much of the volume to take, as a percent).
2. Work out the **volume** the rate applies to (the last closed bar, or only its buyer-side part).
3. Size the child: rate × volume, capped by what is left of the parent and by the best-bid size (peg mode).
4. Price the child (LIMIT at the best bid plus one tick in peg mode; the old entry price otherwise).
5. Send it through the risk gate and the order client. A child that is not filled is cancelled, and in peg mode it may be repriced.

Between runs, the bot polls quotes every 15 seconds (only in modes that need them) to keep the buyer VWAP and buyer share current.

## 2. The two options

Set with `pov_mode` in `config/bot.yaml`. The default is `peg_band_buyer`. Any other value stops the bot at start.

| Option | Entry rate | Entry volume | Entry price | Quote polls |
|---|---|---|---|---|
| `peg_band_buyer` (default) | VWAP band (fixed `pov` if no VWAP) | last bar × buyer share (full bar if no share) | BUY: best bid + one tick, never to the ask; bounded reprice | yes |
| `fixed` | `pov` | full last bar | old entry LIMIT (last close + slippage cap) | no |

**Exits are always fixed:** `pov` on the full last bar, MARKET, never banded, buyer-sized or halted. A band halt must never stall an exit.

**Single fallback (peg mode):** if any entry input is missing (band VWAP, buyer share, depth or best bid), the child uses fixed volume (`pov` × full last bar) and the old entry price.

## 3. The rate: the VWAP band (`pov_band.py`, existing module)

- **Buyer VWAP** (`execution/buyer_vwap.py`): from quote polls. Volume added between two polls counts as buyer-initiated only when LTP ≥ ask at the later poll. VWAP = Σ(LTP × counted volume) ÷ Σ(counted volume), reset every IST day.
- **Band:** ceiling = VWAP × (1 + `vwap_ceiling_pct`/100), floor = VWAP × (1 − `vwap_floor_pct`/100). Defaults 0.5% and 3.0%.
- **Position in band** = (ceiling − price) ÷ (ceiling − floor), clamped to 0–1. The price is the **last closed bar's close**, not a poll price.
- **Rate** = `min_pov` + position × (`max_pov` − `min_pov`). Near the top of the band gives the lower rate; near the bottom gives the higher rate.
- **Above the ceiling:** HALT. Rate 0, so no child this run. The parent waits.
- **Below the floor:** passthrough. Uses `pov` (set as `part_pct`).

Example: VWAP 100.14, so ceiling 100.64 and floor 97.14. LTP 100.20 gives 7.5%. LTP 97.50 gives 22.9%. LTP 101.00 halts. LTP 96.00 uses 10%.

## 4. The volume: buyer share (`buyer_vwap.py`)

- **Buyer volume** = shares counted as buyer-initiated today.
- **Buyer share** = buyer volume ÷ volume that could be classified (intervals with both LTP and ask).
- In `buyer` and the two combined modes, the volume used for sizing = last closed bar volume × buyer share.

## 5. The peg (`execution/peg.py`, `_submit_peg` in `strategy_bot.py`)

Used only in `peg_band_buyer`, and only for BUY entries.

- **Price:** the best bid from the depth reply, plus one tick (`peg_tick_size`, default 0.05). Stays at the best bid if bid + tick would reach the best ask, so it never crosses the spread. Never above the entry cap (last close + `entry_max_slippage_bps`).
- **Best-bid size cap:** a child may take at most `peg_bid_fraction` (default 0.5) of the shares resting at the best bid. A thin bid cuts the child down. A bid with no room stops the peg for that child.
- **Wait:** LIMIT entries wait `peg_wait_seconds` (default 5) before the status check. This applies only in peg mode, through `AnalyzeSender.limit_wait_seconds`.
- **Bounded reprice:** up to `peg_max_attempts` placements (default 3). Reprice only after a clean cancel of an unfilled order ("limit not filled" with cancel success). A partial fill or a failed cancel stops at once and raises the usual alert.
- **Unfilled after all attempts:** the parent stays open for the next run. It is not halted.

## 6. Fallbacks

Peg mode has one fallback rule: if any input is missing, the child uses **fixed volume** (`pov` × full last bar) and the old entry price. Missing inputs are:

- no verified buyer VWAP (band rate unavailable),
- no buyer share (volume unavailable),
- no depth or best bid (peg price unavailable).

In `band`, `buyer`, and `band_buyer`, each part falls back on its own: no VWAP gives the fixed rate, and no buyer share gives the full bar volume. Every fallback is logged (`pov_band_fallback`, `pov_buyer_fallback`, `peg_fallback_fixed`).

Other rules that apply in all modes: a halt skips slicing for that run, and the parent is never closed early because of a halt. It expires only through `max_cycles`.

## 7. Settings (`config/bot.yaml`)

| Key | Default | Range | Used by |
|---|---|---|---|
| `pov_mode` | `peg_band_buyer` | peg_band_buyer, fixed (anything else refused) | all |
| `pov` | 0.1 | (0, 1] | all (fixed rate, passthrough) |
| `pov_band.vwap_ceiling_pct` | 0.5 | > 0 | band modes |
| `pov_band.vwap_floor_pct` | 3.0 | > 0 | band modes |
| `pov_band.min_pov` | 0.05 | (0, 1], ≤ max_pov | band modes |
| `pov_band.max_pov` | 0.25 | (0, 1] | band modes |
| `pov_band.passthrough_floor` | part_pct | part_pct, min_pov, max_pov | band modes |
| `vwap_poll_seconds` | 15 | 5 to 300 | quote polls |
| `peg_tick_size` | 0.05 | 0 to 1 (0 turns it off) | peg |
| `peg_bid_fraction` | 0.5 | (0, 1] | peg |
| `peg_wait_seconds` | 5 | 1 to 60 | peg |
| `peg_max_attempts` | 3 | 1 to 5 | peg |
| `entry_max_slippage_bps` | 20 | (0, 200] | entry cap |

Bad values stop the bot at start (`BotConfig` validation).

## 8. Where the code is

- `src/quant/execution/pov_band.py`: existing VWAP band rule (unchanged).
- `src/quant/execution/buyer_vwap.py`: `BuyerVwapBook` (buyer VWAP, buyer volume, buyer share, poll).
- `src/quant/execution/peg.py`: `peg_level` (best bid, one-tick improvement, best-bid size).
- `src/quant/execution/strategy_bot.py`: `BotConfig` validation, `_pov_for` (rate), `_slice_volume` (volume), `_peg_on`, `_peg_price_now`, `_submit_peg` (bounded reprice), `_slice_parents` (the main loop), `AnalyzeSender` (LIMIT wait).
- `src/quant/execution/pov_executor.py`: `child_qty(..., pov)` takes an optional rate.
- `scripts/run/run_strategy_bot.py`: `run()` calls `bot.vwap_tick` every `vwap_poll_seconds` during the 5-minute sleep.
- `scripts/run/run_strategy_bot_live.py`: wires the quote book, the depth reader, the buyer-share provider, and `_live_sender` (LIMIT wait in peg mode).

## 9. Tests

- `src/quant/tests/test_pov_band_mode_2026_11_15.py`: rate direction, halt, passthrough, fallback, config checks.
- `src/quant/tests/test_buyer_vwap_2026_11_16.py`: buyer classification, VWAP math, volume resets, day change, poll errors, buyer share, buyer slicing.
- `src/quant/tests/test_peg_band_buyer_2026_11_16.py`: peg price, one-tick rule, never to the ask, size cap, bounded reprice, partial and failed-cancel stops, LIMIT wait, validation.

Full suite: 3450 passed, 1 skipped, 1 xfailed, 1 failed. The failure is `tests/unit/test_upgrade_pin.py`, which was already failing on master (vendored copy fingerprint).

## 10. Limits and checks before live

These are not verified against a live or analyzer account. Each must be checked before `pov_mode` is switched on for live.

1. **Quote fields:** `quotes` must return `ltp`, `ask`, and a cumulative `volume` for NSE stocks. Without them, buyer VWAP and buyer share are empty and every mode falls back.
2. **Depth shape:** `depth` must return `data.bids` and `data.asks` lists, each with `price` and `quantity` (or `qty`).
3. **Open LIMIT reply:** the bot detects an unfilled LIMIT by the text "limit not filled". Confirm that wording on the analyzer, or change the check.
4. **Tick size:** confirm 0.05 for each screened stock, or set `peg_tick_size` per stock.
5. **Poll rate:** about 8 symbols every 15 seconds. Confirm OpenAlgo's rate limit.
6. **Restart:** after a restart the quote baseline is set again, so the day's buyer VWAP is partial. Acceptable for now; noted.
7. **Approximation:** the buyer share is the day's share, applied to the last bar.
8. **Owner items (unchanged):** book caps, `live.max_capital_inr`, the analyzer soak, and the kill-switch drill.
