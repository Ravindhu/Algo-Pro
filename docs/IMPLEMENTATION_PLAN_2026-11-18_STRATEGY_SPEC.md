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
