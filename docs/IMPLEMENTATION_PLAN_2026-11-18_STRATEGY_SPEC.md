# Strategy spec and plan (2026-11-18)

## Confirmed by owner (2026-11-18)
1. Trade setup timeframes (MTF, HTF, LTF): common to all four strategies.
2. Strategy pattern rules, as stated by owner:
   - **Engulfing**: both candle bodies more than 50% of their range. The second candle closes above the first candle's high.
   - **OCL**: Red-Red-Green (RRG). Aggressive key level: candle 2 breaks candle 1's open, and candle 3 breaks candle 2's close. Conservative key level: the body of the lowest swing candle.
   - **OCL sell-fail (top)**: a fresh engulfing candle with a body above 50%. The second candle closes above the first candle's low. Later, a candle breaks and closes above the engulfing high with a body above 50%, which means the sell has failed. Two key levels, as in OCL.
   - **OCL sell-fail (bottom)**: same rules as sell-fail top, using the bottom key level. Standalone, not chained to the top strategy.
3. The bot timeframe is every 5 minutes, separate from MTF, HTF and LTF.
4. Each strategy is standalone (plug-and-play). No strategy depends on another.

## Still open (owner to answer before build)
A. Which timeframe carries each pattern? Each strategy has MTF, HTF and LTF. Give the pattern for each slot (engulfing, RRG, sell-fail), or say which slots are unused.
B. Engulfing entry, stop and target. The code uses a 88.6% fib entry, 1.5 x ATR stop and 3 x ATR target. Keep, or change?
C. Sell-fail: which timeframe is the engulfing candle on, and what is the entry after the sell fails?

## Plan (after open items are answered)
1. Strategy plug-in contract: name, the three timeframes, the pattern for each, and a signal function. A registry finds the plug-ins.
2. Pattern checks: separate checks for the engulfing rule, the RRG rule and the sell-fail rule. Reuse existing code. Unit tests for each.
3. Bar feed: closed bars for MTF, HTF and LTF, cached by symbol and interval.
4. Wire each strategy standalone.
5. Bot cycle every 5 minutes. Time limits scaled to 5 minutes.
6. Paper run on real bars, then analyze mode. Live stays off until owner sign-off.
7. Decision note, flow document and diagram updated, as the repo rules require.

## Owner answers, round 3 (2026-11-18)
Pattern per timeframe (MTF / HTF / LTF):
- Engulfing strategy: engulfing / OCL / OCL
- OCL strategy: OCL / OCL / OCL
- Sell-fail top: OCL / OCL / OCL
- Sell-fail bottom: OCL / OCL / OCL

Engulfing entry, stop and target: NOT kept (owner answer "No"). New values still to be given.

Sell-fail trigger: buy once a candle with a 50% body breaks and closes above the sell engulfing pattern's high.

Open:
- The sell-fail table says OCL on all three timeframes, but the sell-fail rule starts from an engulfing candle. Which timeframe holds that engulfing candle?
- New engulfing entry, stop and target values.

## Owner answers, round 4 (2026-11-18)
- Engulfing strategy pattern: same as the OCL pattern (RRG).
- Engulfing key levels: aggressive = open of the 1st candle. Conservative = lowest body of the two candles.
- Sell-fail engulfing candle: red-green engulfing pattern on MTF.

Open:
- Engulfing stop loss and target: not yet given.

## Owner answers, round 4 (2026-11-18)
- Sell-fail engulfing candle: on MTF, as a Red-Green engulfing pattern. This replaces the OCL-on-MTF in the sell-fail table above.
- Engulfing (strategy) entry levels:
  - Aggressive key level: open of the 1st candle.
  - Conservative key level: lowest body of the two candles.
- Engulfing stop loss, take profit, breakeven and trailing: same as the OCL LTF rules.

- Sell-fail stop loss, take profit, breakeven and trailing: same as the OCL LTF rules (owner confirmed 2026-11-18).

Status: spec complete. Ready to build after owner go-ahead.

## Build status (2026-11-19)
- Phase 1 done: `src/quant/strategies/owner_patterns.py` (detection only, no orders or exits) with `tests/unit/test_owner_patterns.py` (11 tests).
  - `is_red_green_engulf`: red bar, then green bar, both bodies more than 50%, green closes above red high.
  - `find_sell_fail_trigger`: sell-fail pattern (green closes above red low, bodies more than 50%), then a green trigger with a body over 50% that closes above the pattern high. An earlier close above the level voids the pattern.
- Assumption to confirm: the sell-fail level is the higher of the two pattern highs.
- Open for phase 2 (OCL key levels): the owner's text says "2nd candle break 1st candle open" and "3rd candle break 2nd candle close", but not the direction of the break. Also, the conservative level is "lowest body of the swing". Which bar counts as the swing, and what is the break direction?
- Not built yet: wiring into the bot, the multi-timeframe bar feed, and the bot timeframe. Those come after the OCL checks.

## Owner answers, round 5 (2026-11-19)
- Sell-fail level: the higher of the two candles' highs (the pattern's two candles only). Matches `find_sell_fail_trigger`.
- OCL key-level breaks: upward direction only. "Candle 2 breaks candle 1 open" and "candle 3 breaks candle 2 close" mean the price closes above those levels.
- Still open: the swing window for the conservative level (how many candles count as the swing).
