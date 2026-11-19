# Strategy rules and logic (2026-11-19)

Flow stage: Strategy (plug-in strategies), Strategy bot framework
Status: Engulfing detection and bot wiring are built (paper only). The other four strategies are specified, not built.
Source: owner rules in `docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md` (rounds 3 to 10). Replaces `STRATEGY_EXPLAINER_2026-11-18.md`, which is out of date.

All rules work on closed bars only. Oldest bar first. The bar in progress is never used.

## 1. Words used

- **MTF**: multi timeframe, the largest of the three. Example: Daily.
- **HTF**: higher timeframe. Example: 4-hour (not yet supported, see section 8).
- **LTF**: lower timeframe, the smallest. Example: 5-minute.
- Order is always LTF < HTF < MTF. Saved in the dashboard Trade setup panel and checked by `check_timeframe_order`.
- **Body**: |close - open|. **Range**: high - low. A candle has a "50% body" when body / range > 0.5.
- **Red** candle: close < open. **Green** candle: close > open.
- **Aggressive key level**: the first level to be touched. Used as the entry level.
- **Conservative key level**: the lowest body bottom of the pattern candles. Used as the stop reference (see section 7).
- **Swing low (3-candle)**: the middle candle's low is below the lows of the candle before and after it.
- **Sweep**: a later bar's low goes below a swing low. A wick break or a body close below it both count.
- **OCL**: the pattern "red, red, green" (RRG) with a break of a key level.
- **egpattern** (red-green engulfing): a red candle, then a green candle that closes above the red high. Both bodies more than 50%.

## 2. The five strategies

| # | Name in the bot | Pattern | MTF | HTF | LTF | Status |
|---|---|---|---|---|---|---|
| 1 | Engulfing | egpattern | egpattern | sweep (3-candle swing low) | OCL, then touch | Detection built, wired to bot (paper) |
| 2 | Engulfing fail | egpattern | egpattern (sell-fail engulf) | sweep | OCL, then touch | Not built |
| 3 | OCL | OCL (RRG) | OCL | OCL | OCL | Pattern checks built, sequence not built |
| 4 | OCL sell-fail top | sell-fail | OCL | OCL | OCL | Pattern checks partly built, not wired |
| 5 | OCL sell-fail bottom | sell-fail (bottom level) | OCL | OCL | OCL | Not wired. Standalone, not chained to 4 |

Each strategy is standalone. None depends on another.

Note: the owner's round 3 table lists OCL on all three slots for sell-fail. Round 4 changed the sell-fail engulf candle to egpattern on MTF. Round 4 is used here.

## 3. Pattern rules

### 3.1 egpattern (red-green engulfing), 2 candles
1. Candle 1 is red, body more than 50% of its range.
2. Candle 2 is green, body more than 50% of its range.
3. Candle 2 closes above candle 1's high.
4. The pattern is confirmed only when candle 2 closes.
- Aggressive key level = open of candle 1.
- Conservative key level = lowest body bottom of the two candles.

### 3.2 OCL (red-red-green), 3 candles, last 3 closed bars
Candles C1, C2 (red) and C3 (green). Two possible breaks. The first break decides.
- **3-candle break**: C3 closes above C2 open. Aggressive key level = C2 open. Checked on C3.
- **4-candle break**: C4 closes above C2 open. Aggressive key level = C3 close. Checked on C4.
- If C3 already broke C2 open, the 4-candle break is ignored.
- Conservative key level = lowest body bottom of C1, C2 and C3.
- A "break" is a candle close above the level. Assumed. Owner to confirm if an intrabar touch was meant.

### 3.3 Sell-fail (top and bottom)
1. A fresh engulfing candle with a body over 50%. The second candle closes above the first candle's low (bottom version) or the matching rule (top version).
2. Level for the sell-fail = the higher of the two pattern candles' highs.
3. Trigger: a later candle with a body over 50% closes above that level. This means the sell has failed. Buy.
4. An earlier close above the level voids the pattern.
- Top and bottom use the same rules. Bottom uses the bottom key level.
- Owner to confirm the exact top-version wording (round 3 lists it as the same rule with the top key level).

## 4. Entry flow (all strategies)

1. The pattern is confirmed only when its bar closes.
2. On the next candle, price touches the AGGRESSIVE key level of that pattern.
3. Next, the sweep is looked for on the next timeframe up (section 5).
4. After the sweep, the OCL on the LTF forms (section 3.2).
5. The next LTF bar touches the OCL's aggressive key level. This touch is the **trigger**. Entry = that key level.
6. The bot checks every 5 minutes. A setup enters once (section 6).

## 5. Sweep mapping (owner confirmed)

- MTF pattern: the sweep is on **HTF** (HTF 3-candle swing low must be swept).
- HTF pattern: the sweep is on **LTF**.
- LTF: no sweep is needed.
- A sweep counts when a wick or a body close goes below the swing low.

Example from owner: MTF Daily aggressive level, HTF 4-hour sweep, LTF 5-minute OCL formed after that sweep. The LTF OCL's own aggressive level, when touched, triggers the trade.

## 6. How the bot finds setups

Every `interval_minutes` (5 minutes, in `config/bot.yaml`):
1. Read the screen file. Use only symbols from a screen at most 10 minutes old, with no ASM/GSM flag.
2. For each symbol, get closed bars for MTF, HTF and LTF (`MultiTimeframeBars`). If any feed is stale or missing, skip that symbol.
3. Run each strategy's check on those closed bars.
4. If a setup triggers, it becomes a parent plan. The parent key is symbol + strategy + side + trigger bar time. The same setup is stored once, however many cycles see it.
5. The parent is then sliced and sent through the risk gate, as with all bot trades.

Important timing facts:
- Checks run on closed bars only. A touch that happens between two cycles is seen at the next cycle. The entry can be a few minutes late, and price may have moved. The entry is a limit order (entry_max_slippage_bps, 20 by default).
- A setup is found in the cycle after its trigger bar closes. With a 5-minute cycle, a 1-minute LTF is seen late.
- Open owner decision: should a trigger count only when it is on the latest closed LTF bar? Today an older trigger still counts if the setup is not already recorded.

## 7. Stop, target, breakeven and trail

Owner rule (round 4): stop, target, breakeven and trailing for every strategy = the OCL LTF rules.

Assumed until owner confirms (implemented for Engulfing only):
- Stop = lowest low of the LTF OCL candles C1..C3.
- Target = 3R (3 times the stop distance).
- ATR = ATR(14) on LTF closed bars. Used by the existing ATR checks.
- Breakeven and trail: global values in `config/bot.yaml` (1R breakeven, 2 ATR trail), unless set per strategy under `exits`.

## 8. Open items

1. 4-hour HTF is not an OpenAlgo interval. It needs 1-hour bars combined into 4-hour bars, aligned to the NSE session start at 09:15. Owner decision.
2. Is the 3-candle break needing no more than C3 green and closing above C2 open? Owner to confirm.
3. Sweep for the LTF setup: the LTF has no sweep, so the rule is settled. Confirm the top sell-fail wording.
4. Freshness rule for triggers (section 6).
5. Stop and target (section 7). Owner to confirm 3R and the stop reference.
6. The Engulfing fail (strategy 2) and the OCL and sell-fail (3 to 5) sequences, stops and targets are not built.

## 9. Where the code is

- Pattern checks and sequences: `src/quant/strategies/owner_patterns.py` (`is_red_green_engulf`, `find_ocl_rrg`, `find_swing_low_sweep`, `find_sell_fail_trigger`, `engulf_sequence`, `engulf_signal`).
- Plug-in strategy: `src/quant/strategies/engulf_sequence_strategy.py`.
- Closed bars for three timeframes: `src/quant/data/mtf_bars.py`.
- Bot cycle: `src/quant/execution/strategy_bot.py`.
- Runners (refuse to start without all three timeframes): `scripts/run/run_strategy_bot.py`, `scripts/run/run_strategy_bot_live.py`.
- Tests: `tests/unit/test_owner_patterns.py`, `tests/unit/test_engulf_signal.py`, `tests/unit/test_mtf_bars.py`, `tests/unit/test_setup_timeframes.py`.
