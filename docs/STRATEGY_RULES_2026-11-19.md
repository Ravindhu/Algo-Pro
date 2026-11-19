# Strategy rules and logic (2026-11-19)

Flow stage: Strategy (sequence strategies), Closed bars MTF/HTF/LTF, Engulf sequence, Strategy bot cycle
Status: all five sequence strategies are built and tested, PAPER ONLY. Live stays off until owner sign-off.
Source: owner rules in `docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md` (rounds 3 to 10). Replaces `STRATEGY_EXPLAINER_2026-11-18.md`, which is out of date.

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

Owner confirmed 2026-11-19: yes. The bot takes a trigger only when the touch is on the newest closed LTF bar. An older touch means the setup is already stale and entering later could be far from the level, so the sequence returns nothing. Implemented as `fresh=True` in `sequence_signal` (the bot path). The tests of the plain sequence keep `fresh=False`.

Consequence: with a 5-minute LTF and a 5-minute bot cycle, every touch is seen once. If the LTF is shorter than the bot interval (for example 1-minute), a touch can happen between two cycles and be missed. The owner should keep LTF at or above the bot interval, or change the rule.

## 6. How the bot finds setups

Every `interval_minutes` (5, in `config/bot.yaml`):
1. Read the screen file. Use only symbols from a screen at most 10 minutes old, with no ASM/GSM flag.
2. For each symbol, get closed bars for MTF, HTF and LTF (`MultiTimeframeBars`). If any feed is stale or missing, skip that symbol.
3. Run each strategy's sequence on those closed bars.
4. If a trigger is on the newest LTF bar, it becomes a parent plan. The parent key = symbol + strategy + side + trigger bar time, so one setup gives one parent.
5. The parent goes through the risk gate, as with all bot trades.

## 7. Entry, stop, target, breakeven and trail

Assumed until the owner confirms (same for all five):
- Entry reference = the LTF OCL aggressive key level that was touched. Actual fills follow the bot's POV slicer (owner 2026-11-19), as for all bot parents.
- Stop = lowest low of the LTF OCL candles C1..C3 (for a 4-candle break, C1..C3 are the three candles before C4).
- Target = 3R.
- ATR = ATR(14) on LTF closed bars.
- Breakeven and trail: global values in `config/bot.yaml` (1R breakeven, 2 ATR trail), unless set per strategy under `exits`.

Owner rule (r4): stop, target, breakeven and trail = the OCL LTF rules. The 3R and the stop reference are assumed.

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
1. Entry = the LTF OCL aggressive key level that was touched (not the touch bar's close or open).
2. Stop = lowest low of the LTF OCL candles C1..C3. For a 4-candle break, C1..C3 are the three candles before C4.
3. Target = 3R (3 times the stop distance).
4. ATR = ATR(14) on LTF closed bars. Used only by the existing ATR checks.
5. Breakeven at 1R and trail at 2 ATR, from `config/bot.yaml`, unless set per strategy.
6. Execution is the bot's POV slicer (owner 2026-11-19). The entry key level is the reference price for sizing and stops, not a single fill price.

Pattern rules
7. A "break" of a level = a candle CLOSE above it, not an intrabar touch.
8. 3-candle OCL break needs only: C3 green and C3 close above C2 open. No extra candle condition.
9. The sell-fail trigger candle (green, body over 50%, closes above the pattern high) is NOT required in the five sequences. Why: the owner's round 10 says the LTF touch of the key level is the trigger. The candle rule exists as `find_sell_fail_trigger`, unused. Owner to say whether to add it as an extra entry condition for the sell-fail strategies.
10. Level for `engulfing_fail` = highest candle body top of the two egpattern candles (max of open and close). Owner 2026-11-19.
11. Top strategy has two key levels (owner 2026-11-19). Aggressive = highest candle body top across the OCL pattern: this is the touch level. Conservative = open of the candle before the trigger (C2 for 3-candle, C3 for 4-candle): recorded in the result as `conservative_level`, not used for the touch or the stop. Owner to say if the conservative level should set the stop.
12. Bottom strategy has one level (owner 2026-11-19): lowest candle body bottom across the OCL pattern, up to the breakout candle (C3 or C4). It is the touch level. No HTF sweep (`NO_SWEEP_KINDS`).
13. A sweep = a later bar's low below the swing low (wick or body). Swing = 3-candle swing low.
14. Swing search looks back 20 HTF bars. The newest sweep is used.

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
