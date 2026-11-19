# Strategy rules and logic (2026-11-19)

Flow stage: Strategy (sequence strategies), Closed bars MTF/HTF/LTF, Engulf sequence, Strategy bot cycle
Status: all five sequence strategies are BUILT for the replay. **The owner risk ladder (section 11) is NOT yet implemented in the bot** — see section 13.
Source: owner rules in `docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md` (rounds 3 to 10) plus owner confirmations 2026-11-19. Replaces `STRATEGY_EXPLAINER_2026-11-18.md`, which is out of date.

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

| # | Bot name | MTF setup | Touch level (MTF) | Code kind | Status |
|---|---|---|---|---|---|
| 1 | `engulfing` | egpattern | open of candle 1 (owner r4) | `egpattern_open` | Built, paper |
| 2 | `engulfing_fail` | egpattern (sell-fail engulf on MTF, owner r4) | highest candle body top of the two candles (owner 2026-11-19) | `egpattern_body` | Built, paper |
| 3 | `ocl_seq` | OCL (RRG) | OCL aggressive key level (owner r4 and r6) | `ocl_key` | Built, paper |
| 4 | `ocl_sell_fail_top_seq` | OCL (RRG) | aggressive level = highest candle body top of the OCL pattern (touched). Conservative level = open of the candle before the trigger (recorded, 2 levels in total). | `ocl_top` | Built, paper |
| 5 | `ocl_sell_fail_bottom_seq` | OCL (RRG) | one level only: lowest candle body bottom of the OCL pattern up to the breakout candle (touched). NO HTF sweep. | `ocl_bottom` | Built, paper |

Each strategy is standalone. None depends on another. The sell-fail sequence and the OCL sequence use the same HTF sweep and LTF OCL steps as Engulfing.

Bot names differ from the owner's names: `ocl_seq` is the sequence version of "OCL". The old `ocl`, `ocl_sell_fail_top`, `ocl_sell_fail_bottom` names still run the older OCL classes (they poll the client). The owner decides whether to retire those.

## 3. Pattern rules

### 3.1 egpattern (red-green engulfing), 2 candles
1. Candle 1 red, body over 50%.
2. Candle 2 green, body over 50%.
3. Candle 2 closes above candle 1's high.
4. Confirmed only when candle 2 closes.

### 3.2 OCL (red-red-green), 3 candles, the last 3 or 4 closed bars
C1 red, C2 red, C3 green. The first break decides:
- **3-candle break**: C3 closes above C2 open. Key level = C2 open. Checked on C3. Pattern = C1..C3.
- **4-candle break**: C4 closes above C2 open. Key level = C3 close. Checked on C4. Pattern = C1..C3, trigger candle = C4.
- If C3 already broke C2 open, the 4-candle break is ignored.
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
11. Top strategy has two key levels (owner 2026-11-19). Aggressive = highest candle body top across the OCL pattern: this is the touch level. Conservative = open of the candle before the trigger (C2 for 3-candle, C3 for 4-candle): recorded in the result as `conservative_level`, not used for the touch or the stop. Owner to say if the conservative level should set the stop.
12. Bottom strategy has one level (owner 2026-11-19): lowest candle body bottom across the OCL pattern, up to the breakout candle (C3 or C4). It is the touch level. No HTF sweep (`NO_SWEEP_KINDS`).
13. A sweep = a later bar's low below the swing low (wick or body). Swing = 3-candle swing low.
14. Swing search looks back 20 HTF bars. The newest sweep is used.

Risk ladder (owner 2026-11-19)
14a. Each OCL entry is sized against the HTF OCL low at **its own price**: `shares = floor(equity x risk / (entry - HTF L))`.
14b. Probe 0.10 % aggressive, +0.15 % conservative (0.25 % per OCL).
14c. On invalidation the unfilled quantity is carried, and the next OCL below gets **remainder + a fresh 0.25 %**.
14d. 0.50 % requires the escalation pair: conservative touch FIRST, then a new OCL. One entry only, no split.
14e. 0.50 % is not permanent. After it, wait for the pair again.
14f. Remainder rolls forward through every new OCL until the HTF is invalidated.

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

All sizing uses the same hard stop (HTF L), and each entry is sized at its OWN price:

```
shares(entry) = floor(equity * risk / (entry_price - HTF L))
```

| Stage | Event | Risk |
|---|---|---|
| Probe, aggressive | an OCL's aggressive level is touched | **0.10 %** |
| Probe, conservative | the same OCL's conservative level is then touched | **+0.15 %** (OCL total 0.25 %) |
| Ladder entry | a new OCL forms BELOW the last invalidated level, its aggressive level is touched | **remaining carried qty + 0.25 %** |
| Conviction entry | after the escalation pair (11.1), the new OCL's aggressive level is touched | **0.50 %** (single entry; no split) |

`equity * risk` is the rupee amount the entry may lose if the HTF L is hit. Because every new OCL sits closer to HTF L, the same risk buys MORE shares — that is the point of the ladder.

### 11.4 Probe stage (0.25 % per OCL)

6. OCL #1 aggressive touch → **0.10 %**.
7. If price later returns to OCL #1's conservative level → **+0.15 %**. Entries are averaged (11.9).
8. A candle closes below OCL #1's low → the OCL is invalidated. **The unfilled quantity is paused, not lost.** The remainder is carried forward.
9. The next OCL that forms BELOW the invalidated level, when its aggressive level is touched → **carried remainder + 0.25 %** at that OCL's own price.
10. Repeat 8-9 for every further invalidation. The remainder **rolls forward through every new OCL until the HTF is invalidated** (owner 2026-11-19).

### 11.5 When trading stops and escalates

11. After the ladder entry on the third OCL (step 5 of the owner's table), **stop opening entries at the probe rate**. Hold and wait.
12. The only thing that opens another position is the **escalation pair**, in this exact order:
    - a conservative level is touched, THEN
    - a new OCL forms after that touch.
13. That new OCL's aggressive touch is a **0.50 %** entry, as a single entry (no 0.35/0.15 split).
14. After that entry, **return to waiting** for the escalation pair to fire again. 0.50 % is never permanent: it is re-earned by the pair every time.
15. A conservative touch that is NOT followed by a new OCL does nothing on its own. The pair needs both, in order.

### 11.6 If the pair never fires

16. If no conservative level is ever touched, the cycle stays at probe risk (0.25 % per OCL, plus the rolling remainder) for its whole life. The cycle ends only at the HTF or MTF invalidation.

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
26. Per-order caps (`max_order_quantity`, `max_order_notional`, open exposure) clamp each child independently. Expect the clamp to bite when an OCL sits close to HTF L: 0.25 % of equity over a ₹1 stop is a large share count, and the cap is what keeps it sane.
27. Average entry = the fill-weighted mean (the bot already keeps this; `pos.entry` is updated on every added child).

### 11.10 Open items on this flow

- **N (the backtested target multiple) is not set.** Until it is, an aggressive-only cycle has no exit. The bot must refuse those trades, not run them.
- **Per-cycle loss ceiling is not set.** Probe 0.25 + ladder 0.25 + several 0.50s can realise well over 1 % of equity in one chopped cycle. Today only the daily-loss cap stops it. The owner has not chosen a per-cycle ceiling.

## 13. What the code does NOT yet do (audited 2026-11-19)

Read this before trusting any backtest number. The replay (`cycle.py`) and the bot
(`strategy_bot._cycle_step`) implemented an EARLIER rule set and do not match
section 11. Each line is a real divergence:

1. **The risk ladder is not implemented.** The bot sizes once, with one global
   `risk_per_trade`. It has no 0.10 / 0.15 split, no carried remainder, and no
   0.50 % conviction stage.
2. **A conservative touch never becomes an order.** `_cycle_step` buys only
   `fresh[0]` (the aggressive entry) and then only unpauses the parent. But
   `cycle._ltf_loop` DOES record the conservative entry and averages it into
   `avg_entry`. So the replay reports fills the bot would never place.
3. **A new-below-OCL entry never becomes an order** either, for the same reason.
4. **Target N is unset.** `cycle._finish` returns `mode="target_pending"` with
   `target=None`. A position that touched only an aggressive level then has NO
   target and NO trail — only the HTF stop would close it. This is fail-OPEN and
   must be fixed before any paper run.
5. **The per-cycle loss ceiling does not exist.** Nothing caps realised losses
   inside one cycle except the daily-loss kill switch.
6. **Section 5 (trigger freshness) is correct only for `fresh_entries`**. The
   older `sequence_signal` / `signal_for` path still exists and is still tested;
   the bot does not use it, so its tests give no assurance about the live path.
7. **Only `engulfing` is enabled** in `config/bot.yaml`. The other four strategies
   exist and can be named, but the bot does not run them.

Fixing order, if the owner wants it:
1. Make `target_pending` refuse the trade (closes the only fail-open hole).
2. Implement the ladder in `_cycle_step` (0.10/0.15, remainder carry, 0.25 re-add,
   the escalation pair, 0.50 re-earned) and size per entry against HTF L.
3. Make `cycle.avg_entry` quantity-weighted with the same sizes, so replay and bot
   agree.
4. Set the per-cycle loss ceiling, then enable the remaining four strategies.
