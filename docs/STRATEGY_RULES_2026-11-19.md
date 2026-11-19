# Strategy rules and logic (2026-11-19)

Flow stage: Strategy (sequence strategies), Closed bars MTF/HTF/LTF, Engulf sequence, Strategy bot cycle
Status: all five sequence strategies are built and tested, PAPER ONLY. Live stays off until owner sign-off.
Source: owner rules in `docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md` (rounds 3 to 10). Replaces `STRATEGY_EXPLAINER_2026-11-18.md`, which is out of date.

All rules use closed bars only. Oldest bar first. The bar in progress is never used.

## 1. Words used

- **MTF**: multi timeframe, the largest of the three. Example: Daily.
- **HTF**: higher timeframe. Example: 4-hour (not supported yet, see section 8).
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
| 2 | `engulfing_fail` | egpattern (sell-fail engulf on MTF, owner r4) | higher of the two highs (ASSUMED, sell-fail level r5) | `egpattern_high` | Built, paper |
| 3 | `ocl_seq` | OCL (RRG) | OCL aggressive key level (owner r4 and r6) | `ocl_key` | Built, paper |
| 4 | `ocl_sell_fail_top_seq` | OCL (RRG) | highest high of the OCL candles (ASSUMED) | `ocl_top` | Built, paper |
| 5 | `ocl_sell_fail_bottom_seq` | OCL (RRG) | lowest low of the OCL candles (ASSUMED) | `ocl_bottom` | Built, paper |

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
3. After the touch, the HTF 3-candle swing low is swept.
4. After the sweep, an LTF OCL forms (section 3.2).
5. An LTF bar after that OCL touches its aggressive key level. This is the **trigger**. Entry = that key level.

Mapping (owner confirmed): MTF sweeps on HTF. HTF sweeps on LTF. LTF needs no sweep. This means the sequence above sweeps on HTF.

## 5. Trigger freshness (owner question, 2026-11-19)

Answer: yes. The bot takes a trigger only when the touch is on the newest closed LTF bar. An older touch means the setup is already stale and entering later could be far from the level, so the sequence returns nothing. Implemented as `fresh=True` in `sequence_signal` (the bot path). The tests of the plain sequence keep `fresh=False`.

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
- Entry = the LTF OCL aggressive key level that was touched. Entry is a limit order (`entry_max_slippage_bps`, 20 by default).
- Stop = lowest low of the LTF OCL candles C1..C3 (for a 4-candle break, C1..C3 are the three candles before C4).
- Target = 3R.
- ATR = ATR(14) on LTF closed bars.
- Breakeven and trail: global values in `config/bot.yaml` (1R breakeven, 2 ATR trail), unless set per strategy under `exits`.

Owner rule (r4): stop, target, breakeven and trail = the OCL LTF rules. The 3R and the stop reference are assumed.

## 8. Open items for the owner

1. Level rules for the sell-fail strategies (levels 2, 4 and 5 in section 2) are my assumptions. Confirm or correct.
2. Stop and target (section 7): confirm 3R and the stop reference.
3. The 3-candle OCL break: confirm it needs nothing beyond C3 green and closing above C2 open.
4. 4-hour HTF is not an OpenAlgo interval. It needs 1-hour bars combined into 4-hour bars aligned to the NSE session start at 09:15. Owner decision.
5. Retire the old `ocl`, `ocl_sell_fail_top`, `ocl_sell_fail_bottom` adapter names, or keep them alongside?
6. Keep the sell-fail trigger candle (section 3.3) as an extra entry condition, or not?

## 9. Where the code is

- Patterns, sequence engine, kinds, signal: `src/quant/strategies/owner_patterns.py` (`SEQUENCE_KINDS`, `sequence`, `sequence_signal`, `engulf_signal`).
- Strategy class: `src/quant/strategies/engulf_sequence_strategy.py` (`SequenceStrategy`, `EngulfSequenceStrategy`).
- Closed bars for three timeframes: `src/quant/data/mtf_bars.py`.
- Bot cycle: `src/quant/execution/strategy_bot.py`.
- Runners (refuse to start without all three timeframes): `scripts/run/run_strategy_bot.py`, `scripts/run/run_strategy_bot_live.py`.
- Tests: `tests/unit/test_owner_patterns.py`, `tests/unit/test_engulf_signal.py`, `tests/unit/test_mtf_bars.py`, `tests/unit/test_setup_timeframes.py`, `src/quant/tests/test_audit_fixes_2026_11_10.py`.
