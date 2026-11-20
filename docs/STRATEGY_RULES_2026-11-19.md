# Strategy rules and logic (2026-11-19)

Flow stage: Strategy (sequence strategies), Closed bars MTF/HTF/LTF, Engulf sequence, Strategy bot cycle
Status (updated 2026-11-20): all five sequence strategies are BUILT and enabled in config/bot.yaml. The owner risk tree (section 11A) is wired into the bot (tree mode). Section 13 is the current code status; section 15 is the step-by-step flow from MTF to the POV slicer.
Source: owner rules in `docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md` (rounds 3 to 10) plus owner confirmations 2026-11-19 and 2026-11-20. `docs/STRATEGY_EXPLAINER_2026-11-18.md` explains the same flow in plain English (updated 2026-11-20).

All rules use closed bars only. Oldest bar first. The bar in progress is never used.

## 1. Words used

- **MTF**: multi timeframe, the largest of the three. Example: Daily.
- **HTF**: higher timeframe. Example: 1-hour (owner decision 2026-11-19, replaces 4-hour).
- **LTF**: lower timeframe, the smallest. Example: 5-minute.
- Order is always LTF < HTF < MTF. Set in the dashboard Trade setup panel and checked by `check_timeframe_order`.
- **Body**: |close - open|. **Range**: high - low. A candle has a "50% body" when body / range > 0.5.
- **Red** candle: close < open. **Green** candle: close > open.
- **Aggressive key level**: the first level price touches. Used as the entry level.
- **Swing low (3-candle)**: the middle candle's low is below the lows of the candle before and after it.
- **Sweep**: a later bar's low goes below a swing low. A wick break or a body close both count.
- **egpattern** (red-green engulfing): candle 1 red with a body over 50%, candle 2 green with a body over 50%, candle 2 closes above candle 1's high.
- **OCL (RRG)**: candles C1 red, C2 red, C3 green. Break rules in section 3.2.

## 2. The five strategies

All five follow the same sequence (section 4). They differ only in the MTF setup and the level that gets touched.

| # | Bot name | MTF setup | Aggressive level (MTF) | Conservative level (MTF) | Code kind | Status |
|---|---|---|---|---|---|---|
| 1 | `engulfing` | egpattern | open of candle 1 (owner r4) | **lowest body bottom of the two candles** (owner 2026-11-19): min(open C1, close C1, open C2, close C2) | `egpattern_open` | Built, paper |
| 2 | `engulfing_fail` | egpattern (sell-fail engulf on MTF, owner r4) | highest candle body top of the two candles (owner 2026-11-19) | **lowest body bottom of the two candles** (owner 2026-11-19) | `egpattern_body` | Built, paper |
| 3 | `ocl_seq` | OCL (RRG) | OCL aggressive key level (owner r4 and r6) | LTF OCL conservative level (lowest body bottom of C1..C3) | `ocl_key` | Built, paper |
| 4 | `ocl_sell_fail_top_seq` | OCL (RRG) | highest candle body top of the OCL pattern (touched) | open of the candle before the trigger (C2 for a 3-candle break, C3 for a 4-candle break) | `ocl_top` | Built, paper |
| 5 | `ocl_sell_fail_bottom_seq` | OCL (RRG) | one level only: lowest candle body bottom of the OCL pattern up to the breakout candle (touched). NO HTF sweep. | None (single level) | `ocl_bottom` | Built, paper |

Each strategy is standalone. None depends on another. The sell-fail sequence and the OCL sequence use the same HTF sweep and LTF OCL steps as Engulfing.

Bot names differ from the owner's names: `ocl_seq` is the sequence version of "OCL". The old `ocl`, `ocl_sell_fail_top`, `ocl_sell_fail_bottom` names still run the older OCL classes (they poll the client). The owner decides whether to retire those.

## 3. Pattern rules

### 3.1 egpattern (red-green engulfing), 2 candles
1. Candle 1 red, body over 50%.
2. Candle 2 green, body over 50%.
3. Candle 2 closes above candle 1's high.
4. Confirmed only when candle 2 closes.

### 3.2 OCL (red-red-green), 3 candles, the last 3 or 4 closed bars
C1 red, C2 red, C3 green. **The first break wins** (owner confirmed 2026-11-19), and there are only two:
- **3-candle break**: C3 closes above C2 open. Key level = C2 open. Checked on C3. Pattern = C1..C3.
- **4-candle break**: C3 did **not** close above C2 open, and C4 does. Key level = C3 close. Checked on C4. Pattern = C1..C3, trigger candle = C4.
- If C3 already broke C2 open, the 4-candle break is never considered. One pattern gives one break.
- Conservative level = lowest body bottom of C1..C3.

### 3.3 Sell-fail
Owner rules (r3 and r5): the sell-fail trigger is a green candle with a body over 50% that closes above the sell pattern's high. Only the newest pattern is checked. An earlier close above the level voids it.
- Built as `find_sell_fail_trigger` (tested). It is not used by the five strategies above. The five use the sequence in section 4 with the levels in section 2. Owner to confirm whether the sell-fail trigger candle should also be required.

## 4. Sequence (all five strategies)

1. The MTF setup is confirmed when its bar closes (section 2 gives the setup and level).
2. A later MTF bar touches the level (low at or below, high at or above).
3. After the touch, the HTF 3-candle swing low is swept. (Not for `ocl_sell_fail_bottom_seq`, which skips this step.)
4. After the sweep, an LTF OCL forms (section 3.2).
5. An LTF bar after that OCL touches its aggressive key level. This is the **trigger**. Entry = that key level.

Mapping (owner confirmed): MTF sweeps on HTF. HTF sweeps on LTF. LTF needs no sweep. This means the sequence above sweeps on HTF.

### 4.1 Where each strategy's conservative level comes from (owner 2026-11-19)

Every strategy has an **aggressive** level (first touch) and a **conservative** level (second,
cheaper touch). They differ only in how each level is measured on the MTF pattern:

| Strategy | Aggressive | Conservative |
|---|---|---|
| `engulfing` | open of candle 1 | **lowest body bottom of the two engulfing candles**: min(open C1, close C1, open C2, close C2) |
| `engulfing_fail` | highest body top of the two candles | **lowest body bottom of the two candles** |
| `ocl_seq` | OCL aggressive key level | LTF OCL conservative level (lowest body bottom of C1..C3) |
| `ocl_sell_fail_top_seq` | highest body top of the OCL pattern | open of the candle before the trigger (C2 for 3-candle, C3 for 4-candle) |
| `ocl_sell_fail_bottom_seq` | lowest body bottom of the OCL pattern | none (single level) |

It is the lowest **body**, not the lowest **low**. A long lower wick does not count. That is the
same rule `ocl_top` and `ocl_bottom` already use for their levels.

## 5. Trigger freshness (owner question, 2026-11-19)

Owner confirmed 2026-11-19: yes. The bot takes a trigger only when the touch is on the newest closed LTF bar. An older touch means the setup is already stale and entering later could be far from the level, so the sequence returns nothing.

Implemented as `fresh_entries()` in the cycle path the bot actually uses — `strategy_bot._cycle_step` → `SequenceStrategy.cycle_for` → `cycle.run_cycles` → `cycle.fresh_entries`. The `sequence_signal` path (`fresh=True`) is a separate, older function that the bot does NOT call.

Consequence: with a 5-minute LTF and a 5-minute bot cycle, every touch is seen once. If the LTF is shorter than the bot interval (for example 1-minute), a touch can happen between two cycles and be missed. The owner should keep LTF at or above the bot interval, or change the rule.

## 6. How the bot finds setups

Every `interval_minutes` (5, in `config/bot.yaml`):
1. Read the screen file. Use only symbols from a screen at most 10 minutes old, with no ASM/GSM flag.
2. For each symbol, get closed bars for MTF, HTF and LTF (`MultiTimeframeBars`). If any feed is stale or missing, skip that symbol.
3. Run each strategy's sequence on those closed bars.
4. If a trigger is on the newest LTF bar, it becomes a parent plan. The parent key = symbol + strategy + side + trigger bar time, so one setup gives one parent.
5. The parent goes through the risk gate, as with all bot trades.

## 7. Entry, stop, target, breakeven and trail

**SUPERSEDED by section 11.7 and 11.9.** Kept only to show the history. The live rule is:

- Entry reference = the LTF level touched (aggressive or conservative, see 11.3). Fills follow the POV slicer.
- **Stop = the HTF OCL low (HTF L)**, the same for every entry in the cycle. NOT the LTF OCL low.
- Target: none if a conservative level was touched (ATR trail). Otherwise `avg + N x R`, and N is still unknown (11.10).
- ATR = ATR(14) on LTF closed bars.
- Breakeven is OFF for cycle positions. Trail applies only after a conservative touch (11.7).

Owner rule (r4): stop, target, breakeven and trail = the OCL LTF rules, refined by the section 11 cycle flow.

## 8. Open items for the owner

1. Level rules for the sell-fail strategies (levels 2, 4 and 5 in section 2) are my assumptions. Confirm or correct.
2. Stop and target (section 7): confirm 3R and the stop reference.
3. The 3-candle OCL break: confirm it needs nothing beyond C3 green and closing above C2 open.
4. HTF: owner decided 2026-11-19 to use 1-hour instead of 4-hour. No 4-hour bar is built. (Closed.)
5. Retire the old `ocl`, `ocl_sell_fail_top`, `ocl_sell_fail_bottom` adapter names, or keep them alongside?
6. Keep the sell-fail trigger candle (section 3.3) as an extra entry condition, or not?

## 9. Where the code is

- Patterns, sequence engine, kinds, signal: `src/quant/strategies/owner_patterns.py` (`SEQUENCE_KINDS`, `sequence`, `sequence_signal`, `engulf_signal`).
- Strategy class: `src/quant/strategies/engulf_sequence_strategy.py` (`SequenceStrategy`, `EngulfSequenceStrategy`).
- Closed bars for three timeframes: `src/quant/data/mtf_bars.py`.
- Bot cycle: `src/quant/execution/strategy_bot.py`.
- Runners (refuse to start without all three timeframes): `scripts/run/run_strategy_bot.py`, `scripts/run/run_strategy_bot_live.py`.
- Tests: `tests/unit/test_owner_patterns.py`, `tests/unit/test_engulf_signal.py`, `tests/unit/test_mtf_bars.py`, `tests/unit/test_setup_timeframes.py`, `src/quant/tests/test_audit_fixes_2026_11_10.py`.

## 10. Assumptions in use (2026-11-19)

Each item below is a rule the code uses that the owner has not explicitly confirmed. Confirm or correct each one.

Entry and exits
1. Entry = the level touched on the LTF (aggressive 0.10 %, then conservative +0.15 %), not the touch bar's close or open. See 11.3.
2. **Stop = the HTF OCL low (HTF L)**, shared by every entry in the cycle. The earlier "LTF OCL low" stop is RETIRED (owner 2026-11-19; it is now only the invalidation price that pauses the OCL).
3. Target = `avg + N x R`, and **N is not set** (11.10). No target at all once a conservative level has been touched (ATR trail instead).
4. ATR = ATR(14) on LTF closed bars. Used by the trail and the existing ATR checks.
5. Breakeven is OFF for cycle positions. The ATR trail applies only after a conservative touch (11.7).
6. Execution is the bot's POV slicer (owner 2026-11-19). The entry level is the reference price for sizing and stops, not a single fill price.

Pattern rules
7. A "break" of a level = a candle CLOSE above it, not an intrabar touch.
8. 3-candle OCL break needs only: C3 green and C3 close above C2 open. No extra candle condition.
9. The sell-fail trigger candle (green, body over 50%, closes above the pattern high) is NOT required in the five sequences. Why: the owner's round 10 says the LTF touch of the key level is the trigger. The candle rule exists as `find_sell_fail_trigger`, unused. Owner to say whether to add it as an extra entry condition for the sell-fail strategies.
10. Level for `engulfing_fail` = highest candle body top of the two egpattern candles (max of open and close). Owner 2026-11-19.
10a. **`engulfing` now has a conservative level too** (owner 2026-11-19): the lowest body bottom of the two engulfing candles, min(open C1, close C1, open C2, close C2). `engulfing_fail` uses the same lowest body bottom as its conservative level. A wick below the body does not count.
11. Top strategy has two key levels (owner 2026-11-19). Aggressive = highest candle body top across the OCL pattern: this is the touch level. Conservative = open of the candle before the trigger (C2 for 3-candle, C3 for 4-candle): recorded in the result as `conservative_level`, not used for the touch or the stop. Owner to say if the conservative level should set the stop.
12. Bottom strategy has one level (owner 2026-11-19): lowest candle body bottom across the OCL pattern, up to the breakout candle (C3 or C4). It is the touch level. No HTF sweep (`NO_SWEEP_KINDS`).
13. A sweep = a later bar's low below the swing low (wick or body). Swing = 3-candle swing low.
14. Swing search looks back 20 HTF bars. The newest sweep is used.

Risk ladder (owner 2026-11-19)
14a. Each OCL entry is sized against the HTF OCL low at **its own price**: `shares = floor(equity x risk / sizing stop)`. The sizing stop is defined in 14a-stop below (owner 2026-11-20).
14a-stop. **Sizing stop (owner 2026-11-20):** `sizing stop = max(ATR floor, (entry - HTF L) + 0.05 % x entry)`, whichever is higher.
   - ATR floor = 1 x ATR(14) on the LTF bar of the touch. **The multiplier 1 is an assumption the owner has not yet confirmed.**
   - The 0.05 % buffer is owner-approved.
   - If ATR is unknown on that bar, no order is placed (fail closed).
   - The 1 % cycle ceiling and the 3 % portfolio ceiling measure planned risk at this same sizing stop.
14b. Probe 0.10 % aggressive, +0.15 % conservative (0.25 % per OCL).
14c. On invalidation the unfilled quantity is carried, and the next OCL below gets **remainder + a fresh 0.25 %**.
14d. 0.50 % requires the escalation pair: conservative touch FIRST, then a new OCL. One entry only, no split.
14e. 0.50 % is not permanent. After it, wait for the pair again.
14f. Remainder rolls forward through every new OCL until the HTF is invalidated.

Order pricing (owner 2026-11-19)
14g. Entry LIMIT price = one tick above the bid price level that carries the **largest resting quantity among the top 5 bid levels**, never at or above the best ask, never above `last close x (1 + entry_max_slippage_bps)`. If no depth is available the bot falls back to `last close x (1 + entry_max_slippage_bps)`.
14h. Implementation: `execution/peg.py:peg_level`, driven by `bot.yaml` `pov_mode: peg_band_buyer` and `peg_tick_size`. The 5-level scan and the highest-quantity pick were added 2026-11-19; before that it used the top of book only.

Sequence scope
15. Only the newest MTF setup is checked. If it is not touched, older setups are ignored.
16. The touch must come after the setup's last candle.
17. The OCL must form after the HTF sweep time. The newest OCL that qualifies is used.
18. Trigger must be on the newest closed LTF bar (owner confirmed 2026-11-19).

Data and bot
19. Closed bars only. Intraday bar fresh if it ended at most 15 minutes ago. Daily bar fresh if at most 4 days old.
20. Screen file at most 10 minutes old. Symbols with ASM/GSM flags are skipped.
21. Long only (BUY). No short side.
22. Parent key = symbol + strategy + side + trigger bar time. One setup gives one parent.
23. Timeframe sweep mapping: MTF sweeps on HTF, HTF sweeps on LTF, LTF needs no sweep (owner confirmed).
24. HTF = 1-hour, LTF = 5-minute (or at least the bot interval), MTF = Daily in the owner's example. These are settings, not hard-coded.
25. Paper mode only. Live stays off.

## 11. Final cycle flow (owner rules, 2026-11-19, revised 2026-11-19). Supersedes sections 4, 6, 7 and 12 where they differ.

Applies to all five strategies. Each strategy starts from its own MTF setup.

### 11.1 Abbreviations used below

- **HTF L** = the HTF OCL low (lowest low of the HTF OCL's C1 to C3). The ONE hard stop of the cycle.
- **OCL** = an LTF OCL (section 3.2). Each OCL has an **aggressive** level and a **conservative** level.
- **Invalidated (LTF)** = a candle CLOSES below that OCL's low (C1..C3). It does not close the position; it pauses it.
- **Escalation pair** = (1) a conservative level is touched, THEN (2) a new OCL forms after that touch. Both, in that order.

### 11.2 The chain (once per cycle)

1. MTF pattern forms. Its aggressive key level is the MTF level.
2. Price touches the MTF aggressive key level.
3. HTF sweep. One sweep is enough for the whole cycle (see 11.7).
4. After the sweep, price touches the HTF aggressive key level.
5. Wait for an LTF OCL. Its aggressive key level is touched: **entry 1**.

### 11.3 The four risk numbers

> **Superseded for sizing by section 11A (owner 2026-11-20).** The numbers below are the old 0.10 / 0.15 / 0.25 % ladder, kept for history.

All sizing uses the same hard stop (HTF L), and each entry is sized at its OWN price. From 2026-11-20 the distance used is the sizing stop (section 14a-stop), not the plain distance to HTF L:

```
shares(entry) = floor(equity * risk / sizing_stop(entry))
sizing_stop(entry) = max(1 x ATR(14), (entry_price - HTF L) + 0.0005 x entry_price)
```

| Stage | Event | Risk |
|---|---|---|
| Probe, aggressive | an OCL's aggressive level is touched | **0.10 %** |
| Probe, conservative | the same OCL's conservative level is then touched | **+0.15 %** (OCL total 0.25 %) |
| Ladder entry | a new OCL forms BELOW the last invalidated level, its aggressive level is touched | **remaining carried qty + 0.10 %** (its conservative touch adds +0.15 %; each OCL totals 0.25 %, owner 2026-11-20) |
| Conviction entry | after the escalation pair (11.1), the new OCL's aggressive level is touched | **0.50 %** (single entry; no split) |

`equity * risk` is the rupee amount the entry may lose if the HTF L is hit. Because every new OCL sits closer to HTF L, the same risk buys MORE shares — that is the point of the ladder.

### 11.4 Probe stage (0.25 % per OCL)

6. OCL #1 aggressive touch → **0.10 %**.
7. If price later returns to an OCL's conservative level (any OCL that has an entry, including the third) → **+0.15 %** on that same OCL. Each OCL totals **0.25 %** (0.10 % + 0.15 %). Entries are averaged (11.9).
8. A candle closes below OCL #1's low → the OCL is invalidated. **The unfilled quantity is paused, not lost.** The remainder is carried forward.
9. The next OCL that forms BELOW the invalidated level, when its aggressive level is touched → **carried remainder + 0.10 %** at that OCL's own price (owner 2026-11-20: the 0.25 % per OCL includes the 0.15 % conservative add in step 7).
10. Repeat 8-9 for each invalidation, up to the third OCL entry (11.5). After the third OCL, the carried remainder waits for the escalation pair (owner 2026-11-20, option A). It is added to the 0.50 % entry only.

### 11.5 When trading stops and escalates

11. After the ladder entry on the third OCL (step 5 of the owner's table), **stop opening entries at the probe rate**. Hold and wait.
12. The only thing that opens another position is the **escalation pair**, in this exact order:
    - a conservative level is touched, THEN
    - a new OCL forms after that touch.
13. That new OCL's aggressive touch is a **0.50 %** entry, as a single entry (no 0.35/0.15 split).
14. After that entry, **return to waiting** for the escalation pair to fire again. 0.50 % is never permanent: it is re-earned by the pair every time.
15. A conservative touch that is NOT followed by a new OCL does nothing on its own. The pair needs both, in order.

### 11.6 If the pair never fires

16. The cycle keeps its probe-rate entries (0.25 % per OCL, up to the third OCL) and any carried remainder waits for the pair. It ends only at the HTF or MTF invalidation.

### 11.7 Stop, average and exits

17. There is no stop below the LTF OCL low. The only hard stop is **HTF L**, and it covers every entry taken from that HTF OCL.
18. An LTF invalidation does not close the position and does not raise the stop. It pauses the unfilled quantity and arms the next OCL (11.4).
19. Average entry = the quantity-weighted mean of the actual POV fill prices (11.9).
20. Exit rules (owner 2026-11-19):
    - If a CONSERVATIVE level was touched anywhere in the cycle: no fixed target. Trail with ATR, as far as the trail allows.
    - If only AGGRESSIVE levels were touched: book at the backtested target. Target = average entry + N x R, where R = average entry - HTF L. **N is not yet known; it comes from the backtest.** Until N is set the bot must refuse the trade rather than run a position with no exit rule.

### 11.8 Invalidation

21. **HTF invalidated**: an HTF candle closes below HTF L. The cycle ends. Wait for a new HTF OCL below that price, its aggressive touch starts the LTF loop again (steps 5 to 15). No new sweep (11.7).
22. **MTF invalidated**: an MTF candle closes below the MTF pattern low. The whole cycle ends. Nothing is carried into a new MTF setup.

### 11.9 Quantity and POV handover (replaces section 12)

23. Every entry is a separate quantity, gated at its own price and size. The entry price is the reference for sizing; actual fills follow the POV slicer.
24. A gated child that is partly filled when the OCL is invalidated keeps its **unfilled** quantity. That remainder is added to the next OCL's new risk slice (11.4 step 9).
25. Later entries do NOT re-use an earlier slice: the remainder is **carried AND a fresh 0.25 % is added** (owner 2026-11-19).
26. Per-order caps (`max_order_quantity`, `max_order_notional`, open exposure) clamp each child independently. Shares a cap blocks are carried to the next OCL, not dropped (owner 2026-11-20). Each clamp is logged as `ladder_clamp`. Expect the clamp to bite when an OCL sits close to HTF L: 0.25 % of equity over a ₹1 stop is a large share count, and the cap is what keeps it sane.
27. Average entry = the fill-weighted mean (the bot already keeps this; `pos.entry` is updated on every added child).

### 11.10 Open items on this flow

- **N (the backtested target multiple) is not set.** Until it is, an aggressive-only cycle has no exit. The bot must refuse those trades, not run them.
- **Superseded by 11A (the symbol allocation and the 5 % portfolio cap replace these).** Loss ceilings were set (owner 2026-11-19 and 2026-11-20): 1 % of equity planned risk per HTF cycle, and 3 % of equity planned risk across all open cycles (all symbols). Each ceiling refuses the order that would exceed it, and is fail closed when unset.

## 13. Code status (updated 2026-11-20)

This replaces the 2026-11-19 audit. Status of each gap found then:

| # | 2026-11-19 gap | Status 2026-11-20 |
|---|---|---|
| 1 | Risk ladder not in the bot | **Done for the owner tree (11A).** The bot sizes each touch from `risk_tree.plan`. The old 0.10 / 0.15 ladder still runs when no symbol allocation is set (not the live config). |
| 2 | A conservative touch never became an order | **Done.** Conservative leaf touches are orders in tree mode. |
| 3 | A new-below-OCL entry never became an order | **Done.** Carried shares plus the new OCL's touch become an order (`RiskLadder` carry, `tree_order`). |
| 4 | Target N unset (fail open) | **N = 2.1** in `settings.yaml` (`cycle.target_r`). With N unset the trade still refuses (`target_pending`). But see issue A in 15.8: the target and trail depend on the old 1 % ladder. |
| 5 | No per-cycle loss ceiling | **Replaced by 11A:** symbol allocation (4 %) and portfolio cap (5 %). The 1 % `cycle.loss_ceiling` is still set and still used by the replay (issue A). |
| 6 | Trigger freshness | Still true: only a touch on the newest closed LTF bar becomes an order (`fresh_entries`, `_scan_cycle` skips stale touches). |
| 7 | Only engulfing enabled | **Done.** All five strategies are listed in `config/bot.yaml`. |

Open issues found while writing section 15 are in 15.8.

## 14. Worked example of the ladder (owner 2026-11-19)

After the HTF OCL has formed. This is the same ladder as section 11.4 and 11.5, written as a
step-by-step table so the implementation can be checked against it line by line.

| # | Event | Size |
|---|---|---|
| 1 | OCL #1 aggressive touch | 0.10 % |
| 1b | OCL #1 conservative touch | +0.15 % (OCL #1 total 0.25 %) |
| 2 | close below OCL #1 low → invalidated | remaining unfilled qty paused |
| 3 | OCL #2 forms below → aggressive | remaining qty + 0.25 % at OCL #2's price |
| 3b | OCL #2 conservative touch | +0.15 % |
| 4 | OCL #2 invalidated | remaining rolls forward |
| 5 | OCL #3 forms below → aggressive | remaining qty + 0.25 % at OCL #3's price |
| 5b | after that entry, stop opening at 0.25 % | wait |
| 6 | wait for: conservative touch, then a new OCL | — |
| 7 | that new OCL → entry | 0.50 % |
| 7b | then back to waiting for the same condition | repeat |
| 8 | HTF close below HTF OCL low | cycle ends |
| — | remainder | rolls forward to every new OCL until HTF invalidation |

Worked numbers (equity ₹1,00,000, HTF L ₹93, OCL #1 agg ₹100 / cons ₹95, OCL #2 agg ₹94).
**Note (2026-11-20):** these numbers use the plain distance (entry − HTF L), the form before the sizing stop. With the sizing stop, the share counts change; the formula is the rule, not these numbers.

```
OCL #1 agg  : floor(1,00,000 x 0.0010 / (100-93)) = 14 sh
OCL #1 cons : floor(1,00,000 x 0.0015 / (95-93))  = 30 sh   (OCL #1 total 0.25 %)
   44 sized; say 20 filled before the close below the OCL #1 low -> 24 remain
OCL #2 agg  : 24 carried + floor(1,00,000 x 0.0025 / (94-93)) = 24 + 250 = 274 sh
```

The 250 is correct arithmetic, not a bug: a ₹1 stop distance makes 0.25 % of equity a large
share count. `max_order_quantity` / `max_order_notional` clamp it at the gate (11.9 item 26),
which is why those caps must stay set before any paper run.
## 11A. Owner risk tree (owner 2026-11-20, confirmed top-down)

Supersedes the fixed 0.10 / 0.15 / 0.25 % ladder numbers and the 1 % per-cycle and 3 % portfolio ceilings in
sections 11.3 and 11.10. The ceilings are replaced by the symbol allocation and the portfolio cap.

- **Portfolio cap:** 5 % of equity across all symbols (settings.yaml cycle.portfolio_loss_ceiling = 0.05).
- **Symbol allocation:** a percent of equity per symbol, from the portfolio engine. Reference 4 %
  (settings.yaml cycle.symbol_allocation_pct). Every budget below scales from it: at 1 %, each value is a quarter.
- **Split of every level:** 5 equal slots of 20 % each: 3 AGGRESSIVE, then WAIT for the conservative touch,
  then 2 CONSERVATIVE. The aggressive share is settings.yaml cycle.aggressive_share, default 0.60, allowed
  0.50 to 0.80 (refused outside). Dashboard Trade setup popup: "Aggressive share" field, 50 to 80 %.
- **MTF level:** 5 HTF OCL slots of 0.8 % (symbol 4 %): 3 aggressive, wait for the MTF conservative key-level
  touch, then 2 conservative. A slot fires when a new HTF OCL forms (a conservative touch arms the next one).
- **HTF level (inside each HTF OCL):** 5 LTF OCL slots of 0.16 %: 3 aggressive, wait for the HTF conservative
  key-level touch, then 2 conservative.
- **LTF level (each LTF OCL):** its budget splits 60/40 between its own aggressive touch and its own
  conservative touch of the same OCL (at the default: 0.096 % and 0.064 %). It uses the same aggressive share.
- **Stop for every level:** HTF L (owner). Sizing: shares = floor(equity x budget / sizing stop) + carry, where the
  sizing stop is section 14a-stop.
- **Carry:** unfilled shares roll forward to the next OCL at the same level, including across HTF OCLs inside one
  MTF setup. Carried shares add no new budget.
- **Forfeit:** an unused slot budget does not move to another slot.
- **Refusal:** a touch with no tree decision (its OCL slot was refused, or the level is waiting) sends nothing.

Code: src/quant/strategies/risk_tree.py (budgets, SlotGate, plan), cycle.py run_cycles (tree events),
strategy_bot.py _scan_cycle (tree branch, carry pool), setup_store.py (aggressive_share), dashboard popup.

---

## 15. Step-by-step flow, MTF to POV slicer (as coded, 2026-11-20)

Each step names the function that does it. The plain-English version is `docs/STRATEGY_EXPLAINER_2026-11-18.md`.

### 15.1 Once per bot cycle (every `interval_minutes`, 5 in bot.yaml)

1. `StrategyBot.cycle` (`strategy_bot.py`): check broker stops and resync them. If the data source is not approved, stop here and flag open positions as unmanaged.
2. Read the screen: only symbols from a screen at most 10 minutes old, with no ASM/GSM flag (`read_screen`).
3. Tree mode only: compute each symbol's allocation with `symbol_allocations` (portfolio allocator: 5 % portfolio, 4 % symbol cap, equal scores). One symbol = 4 %, two = 2.5 % each, five = 1 % each.
4. Run the entry guard and check startup reconciliation. If entries are blocked, the symbol list is emptied: no new setups, but exits and slicing still run.
5. For each symbol: read closed MTF, HTF and LTF bars (`MultiTimeframeBars`). A missing feed skips the symbol and is counted by the feed-health check. Read daily stats.
6. For each enabled strategy, call `_scan` then `_scan_cycle`.

### 15.2 Setup detection (`cycle.run_cycles`, a stateless replay of the whole history)

7. **MTF setup** (`_newest_setup`, `owner_patterns.py`): the newest confirmed MTF pattern for the strategy (egpattern for Engulfing, sell-fail egpattern for `engulfing_fail`, OCL for the three OCL strategies). It gives the MTF level.
8. **MTF touch** (`_mtf_touch`): the first later MTF bar whose range covers that level.
9. **MTF invalidation**: the first later MTF close below the pattern low (`setup_low`). The whole replay ends there.
10. **MTF conservative touch** (`_mtf_cons_touch_time`): the first bar after the MTF touch, before MTF invalidation, whose range covers the setup's conservative level. Emits `mtf_cons_touch`.
11. **HTF sweep** (`first_sweep_after`): an HTF swing low swept after the MTF touch. The replay starts from the sweep bar. `ocl_sell_fail_bottom_seq` (`NO_SWEEP_KINDS`) skips this and starts at the MTF touch.
12. **HTF OCL** (`_htf_stage` with `find_ocl_rrg`): the first HTF OCL that forms at or after the start. A later HTF bar must then touch its key level. After an HTF invalidation, the key must be below the invalidated low (no new sweep). Emits `htf_ocl` at the key time.
13. **HTF conservative touch** (`_htf_cons_touch_time`): the first HTF bar after the key bar, before HTF invalidation, whose range covers the HTF OCL's conservative level. Emits `htf_cons_touch`.
14. **HTF L** (`_htf_low`): the lowest low of the HTF OCL's bars. This is the hard stop for every entry in the cycle.
15. **HTF invalidation** (`_htf_invalid_time`): the first later HTF close below HTF L. The HTF cycle ends.

### 15.3 LTF loop (`_ltf_loop`, from the key touch until the cycle ends)

16. **LTF OCL forms** (`find_ocl_rrg` on LTF bars): a new OCL. After a stop-out it must form with its key below the previous stop-out low. Emits `ltf_ocl`.
17. **LTF aggressive touch**: the first bar whose range covers the OCL's aggressive key level. Emits `ltf_agg_touch`.
18. **LTF conservative touch**: only after that OCL's aggressive touch, the first bar whose range covers its conservative level. Emits `ltf_cons_touch`. From then on the cycle exits in trail mode.
19. **LTF invalidation**: a later LTF close below the OCL's low (C1 to C3). Emits `ltf_invalidated`. The position is paused, not closed, and step 16 must then form a lower OCL.
20. **HTF invalidated**: the loop stops and a new HTF cycle starts (step 12, below the invalidated price). **MTF invalidated**: the loop stops and the whole replay ends.

### 15.4 Risk tree decisions (`risk_tree.plan`, run inside `run_cycles`)

21. All MTF, HTF and LTF events are sorted by time. At equal times, higher-level events (`mtf_cons_touch`, `htf_ocl`, `htf_cons_touch`, `htf_invalidated`) come first.
22. The planner gives each touch a decision: a stage, a budget fraction, or a refusal. Budgets at the 4 % reference and 60 % aggressive share:
    - MTF level, one slot per new HTF OCL: 0.8 %. Five slots: three aggressive, then wait for the MTF conservative touch, then two conservative.
    - HTF level, one slot per new LTF OCL inside an HTF OCL: 0.16 %. Five slots: three aggressive, then wait for the HTF conservative touch, then two conservative.
    - LTF leaf (the 0.16 % of each LTF OCL): 60 % to its aggressive touch (0.096 %), 40 % to its conservative touch (0.064 %).
23. A conservative touch arms the next slot at its level (`SlotGate`). The next OCL to form takes the armed slot. Aggressive slots need no arming. Unused slot budget is forfeited.
24. Every decision is stored on each result (`tree_decisions`). A touch with no decision is refused.

### 15.5 Bot order building (`_scan_cycle`)

25. Only touches on the newest closed LTF bar are used. Older touches are logged as `cycle_event_stale` and skipped.
26. Look up the touch's decision by event, OCL id and time. No decision, or a refusal: logged as `tree_refused`, nothing sent.
27. Size on a trial copy of the ladder (`RiskLadder.tree_order`): shares = floor(equity x fraction / sizing stop) + carried shares. Sizing stop = max(1 x ATR(14) from the cycle view, (price − HTF L) + 0.05 % of price). Carried shares add no budget.
28. Checks, in order: portfolio cap (planned risk at the HTF stops across all open cycles, `_portfolio_ceiling_check`), per-order share and notional caps, the ADV cap (`_adv_cap_qty`), then the risk gate (`_gate_entry`). A clamped shortfall is carried and logged as `ladder_clamp`.
29. A parent is created once per HTF cycle (keyed by symbol, strategy, side and HTF key time), with `remaining` = carried shares + new shares. Its stop = HTF L, its target comes from the replay, and its allocation is locked (`allocation_pct`).
30. A conservative touch sets `parent.trail = True`. From then on the exit is the ATR trail, with no fixed target.
31. The MTF carry pool is keyed by the MTF setup (`_mtf_id`). When an HTF cycle closes with unfilled shares (`_close_htf`), they go into the pool. The pool is used only when the next order is placed in the same MTF setup. If the setup ends first, the pool is dropped.

### 15.6 POV slicer (`_slice_parents`, runs each cycle after the exit check)

32. Skip a parent if a fault is open, the parent is paused (after an LTF invalidation), or it has no remaining shares. A cycle parent waits for its next ladder order and never expires by cycle count (`max_cycles` applies only to non-cycle parents).
33. Take the oldest ladder order with unfilled shares (FIFO). Its price is the reference price for the slice.
34. POV rate (`_pov_for`, entries only). The buyer-initiated VWAP comes from `BuyerVwapBook`, which polls quotes every 15 seconds and counts volume only when LTP ≥ ask.
    - Above the ceiling (VWAP + 0.5 %): **halt**, no child this cycle.
    - Below the floor (VWAP − 3 %): **passthrough** at the base 10 %.
    - In between: the rate moves linearly from 5 % (at the ceiling) to 25 % (at the floor).
    - No VWAP yet: fixed 10 %, logged as `pov_band_fallback`.
35. Volume: the last closed LTF bar's volume x today's buyer share (`_slice_volume`). No share yet: full bar volume (`pov_buyer_fallback`).
36. Child quantity (`child_qty`): POV x volume, capped at the parent's remaining shares and the active order's unfilled shares. In peg mode also capped at a fraction of the best-bid size. Less than 1 share: nothing is sent.
37. Entry execution, default `peg_band_buyer`: a LIMIT at best bid plus one tick, capped at the reference price. It waits 5 seconds and is placed at most 3 times. An unfilled LIMIT is cancelled cleanly and the parent stays open. If no peg price is available: fixed 10 % of full bar volume, at the entry price.
38. Exits are never banded, buyer-sized or halted. They always use the fixed POV.
39. Reply handling: success → `_book_child`: `remaining -= qty`, the ladder records the fill, open exposure is booked. A partial fill (2026-11-20 fix) → the filled shares are booked from the reply's `filled_qty` and `avg_price`, and the parent stays open for the rest. If the cancel of the rest failed → a CRITICAL alert and a fault that blocks entries until checked. Any other non-success → the parent is closed.
40. The average entry (`pos.entry`) is the fill-weighted mean of the child fills, as the bot books them.

### 15.7 Exits (`_manage_exits`, on each position's last closed bar)

41. Stop first: if the bar's low reaches the stop, the exit is at min(stop, open). At the start the stop is HTF L.
42. Target: if a target is set (no conservative touch yet), exit at the target when the bar's high reaches it.
43. Breakeven is off for cycle positions (`breakeven_enabled = not p.cycle`).
44. Trail (after a conservative touch, `trail_from_start`): stop = max(stop, best high − trail_mult x ATR). Defaults: `trail_mult` 2.0, `breakeven_r` 1.0 (unused for cycles). The stop only moves up.
45. A moved stop is logged as `stop_moved`, and the broker SL-M stop is re-placed to match (`_sync_broker_stop`).

### 15.8 Known issues and status (updated 2026-11-20)

- **A. Fixed.** With the tree on, the exit mode and target no longer depend on the old 1 % ladder. `_tree_finish` in `cycle.py` sets them from the tree's sizes. Test: `test_tree_exit_mode_does_not_depend_on_the_old_loss_ceiling`.
- **B. Fixed.** The average entry and target use the tree's sizes (same `_tree_finish`). Test: `test_tree_average_is_weighted_by_the_tree_sizes`.
- **C. Fixed in code, not yet tested on the paper broker.** A partial LIMIT fill now reports `filled_qty` (OpenAlgo `filled_quantity`) and `avg_price`. The bot books those shares (`_book_child`) and keeps the rest of the parent open. If the cancel failed, a fault blocks entries until checked. Tests: `test_partial_entry_fill_is_booked_and_the_rest_stays_open`, `test_partial_fill_with_failed_cancel_raises_a_fault`.
- **D. Open, owner decision.** No profit lock before a conservative touch. The only protection is HTF L. Any lock needs your decision and out-of-sample evidence first.
- **E. Fixed.** The entry signal's `tp_dist` is now 1R. It is a placeholder that only the gate copies, so it looks like no target. Exits never read it.
- **F. Open, owner decision.** Unfilled shares are dropped when their MTF setup ends (forfeit rule). Please confirm this is the rule you want.
