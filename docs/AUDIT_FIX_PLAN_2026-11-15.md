# Audit fix plan, 2026-11-15

Plain-English plan for the live-readiness audit fixes. Live trading is still OFF.
`config/bot.yaml` keeps `mode: paper` and `live_enabled: false`. Nothing in this plan turns live on.

Baseline before any change (commit 25055bf): 3376 passed, 1 skipped, 1 xfailed,
1 failed (`tests/unit/test_upgrade_pin.py`, a vendored-copy fingerprint, already failing on master).

## Items (from the audit)

| # | Item | Status after this work |
|---|---|---|
| 1 | No stop-loss at the broker (bot-side stops only) | Fixed in code: an SL-M stop order is placed at the broker when a position opens, moved when it grows, cancelled before a bot exit, and checked each cycle. |
| 2 | Restart forgets open positions and open exposure | Fixed in code: positions, broker stop ids and exposure are saved to `positions.json` on every change and restored at start, before reconciliation. |
| 3 | Two live entry points (`run_live.py` main still runs the old pipeline) | Fixed in code: `run_live.py` main() refuses to run and points to the bot runner. `startup_checks` stays (the bot and the dashboard use it). |
| 4 | Owner values still null (book caps, egress IP, capital cap) | Owner action. No value is chosen by code. Live keeps refusing until set. |
| 5 | SEBI items in the quant algo | Removed from the live gate messages, config comments, live plan docs, the checklist file, and the event keyword list. The SEBI turnover fee in the cost model is kept (it is a real charge). Vendored OpenAlgo code is not touched. |
| 6 | Entries use MARKET orders with no price limit | Fixed in code: entries use LIMIT orders with a max slippage band (`entry_max_slippage_bps`, default 20). Exits stay MARKET so they always try to fill. An unfilled entry is cancelled. |
| 7 | Doc drift (bot.yaml comment, flow SVG wording, live plan docs) | Fixed in docs: comments corrected; flow doc matches the new behaviour. |
| 8 | Egress IP checked only at start | Fixed in code: re-checked every cycle in live mode. A mismatch blocks new entries (exits still run) and raises a CRITICAL alert. |
| 9 | Found during the work: every order omitted `exchange`, which OpenAlgo's placeorder requires. Live and analyze orders would have been rejected. | Fixed in code: the client sends `exchange` (NSE by default; BSE allowed) on every order. |
| 10 | Found during the work: the live runner never passed the P&L store, so daily loss counters reset on restart. | Fixed in code: the live runner passes `PnlStore` (`data/bot_live/pnl_state.json`). |
| 11 | Requested: breakeven and trail per strategy. Before, every strategy used the global 1R and 2 ATR. | Done: `exits:` block in `config/bot.yaml` sets breakeven_r and trail_mult per strategy. Unlisted strategies use the global values. Unknown names or bad values are refused at start. |
| 12 | Found while doing 11: the broker SL-M stayed at the original stop when breakeven or the trail moved the bot's stop. | Done: a moved stop cancels and re-places the broker SL-M at the new trigger. If re-placing fails, new entries are blocked and the bot-side stop still protects. |
| 14 | Requested: buyer-initiated VWAP for each screened stock from market data | Done in code. Built from quote polls (LTP, ask, cumulative volume), counted only when LTP >= ask. Feeds band mode. Needs the analyzer check of `ask` and `volume` before live. |
| 13 | Requested: band-based POV slicing (high of band = lower POV, low of band = higher POV). Blocked for buyer VWAP and buyer volume (no verified ask or trade tape). | Done in code behind `pov_mode: band`, default stays `fixed`. Band uses the existing `pov_band.py` rule with the last closed close. With no verified buyer VWAP it falls back to `pov`. PEG and buyer-initiated volume are not built; see `docs/PEG_AND_BUYER_VOLUME_DESIGN.md`. |

## Design notes

- Stop and cancel calls use the same session guard as `cancel()` (live approval and limits hash, or analyzer on). Stops only reduce risk, so they need no GateDecision. Owner to confirm.
- Live refuses to start without a stop placer (fail closed). Paper mode has no broker, so it has no stop placer.
- Restore order at start: load positions, then reconcile against the broker book. Entries stay blocked until reconciliation passes.
- Not restored after a restart: parents still being sliced. Their filled quantity is kept; the rest is not finished.
- Known limit kept open: a partial fill on a LIMIT entry is flagged and entries stay blocked until the owner reconciles with the broker. The exact OpenAlgo partial-fill reply shape is not verified; it is matched on the word 'partial' in the order state. Owner to check on the analyzer before live.
- SL-M stop behaviour (trigger, fill price) is taken from the OpenAlgo SDK docs, not yet tested on a live or sandbox account.

## Phases

- [x] P0 Baseline test run recorded (above).
- [x] P1 SEBI removal (code messages, config comments, docs, checklist file deleted). Cost-model SEBI turnover fee kept.
- [x] P2 `run_live.py` main() refuses to run (item 3). `startup_checks` kept for the bot and dashboard.
- [x] P3 Position store and restore (`execution/position_store.py`), P&L store wired in live (item 2, item 10).
- [x] P4 Broker SL-M stops: placed at fill, moved when the position grows, cancelled before a bot exit, checked every cycle (item 1). Exchange field added to every order (item 9).
- [x] P5 LIMIT entries with `entry_max_slippage_bps` (default 20); unfilled entries cancelled; partial fills flagged (item 6).
- [x] P6 Per-cycle egress IP guard blocks new entries only (item 8).
- [x] P7 Doc drift: `bot.yaml` comment, dashboard flow copy, live docs (item 7).
- [x] P9 Per-strategy breakeven and trail (`exits:`); broker stop follows the moved stop.
- [x] P14 Bounded peg (`pov_mode: peg_band_buyer`): best-bid LIMIT, max reprices, wait, single fixed fallback. 9 tests in `src/quant/tests/test_peg_band_buyer_2026_11_16.py`. Analyzer check of the open-LIMIT reply still needed.
- [x] P13 Combined band and buyer mode (`pov_mode: band_buyer`), each part with its own fallback. PEG not built (owner decision). 3 more tests.
- [x] P12 Item 3: buyer-initiated volume and buyer share; `pov_mode: buyer` sizes children from buyer-side volume. 4 more tests in `src/quant/tests/test_buyer_vwap_2026_11_16.py`.
- [x] P11 Buyer-initiated VWAP per screened symbol from quote polls (`execution/buyer_vwap.py`), fed to band mode. 9 tests in `src/quant/tests/test_buyer_vwap_2026_11_16.py`.
- [x] P10 Band POV mode (`pov_mode: band`, default fixed). Design note `docs/PEG_AND_BUYER_VOLUME_DESIGN.md`. 12 tests in `src/quant/tests/test_pov_band_mode_2026_11_15.py`.
- [x] P8 Full test run: 3415 passed, 1 skipped, 1 xfailed; 1 failed (`test_upgrade_pin`, pre-existing, vendored fingerprint). 27 new tests in `src/quant/tests/test_audit_fixes_2026_11_15.py`.

Owner items after this work (not code):
- Item 4: set the four book caps, `live.allowed_egress_ip` and `live.max_capital_inr`. No value is chosen by code.
- Confirm the current retail algo rules with the broker (the SEBI checklist was removed from scope, which does not remove the legal obligations).
- Run the analyze-mode soak on the owner's machine, including a partial-fill test, and the kill-switch drill on the live host.
- Confirm the OpenAlgo partial-fill reply shape and the SL-M behaviour on the analyzer before live.
