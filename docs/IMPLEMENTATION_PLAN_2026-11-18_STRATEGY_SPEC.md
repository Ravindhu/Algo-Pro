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

## Build status: OCL check (2026-11-19)
- `find_ocl_rrg` in `src/quant/strategies/owner_patterns.py`, using the last 3 closed bars only.
  - Pattern: red, red, green.
  - Aggressive key level 1: C1 open. C2 must close above it.
  - Aggressive key level 2: C2 close. C3 must close above it.
  - Conservative key level: the lowest body bottom among the three candles.
- Tests: 17 total in `tests/unit/test_owner_patterns.py`, all passing.
- Assumption: "break" means a close above the level (as stated in round 5). Confirm if you meant an intrabar touch.
- Not built yet: the entry trigger and order logic for each strategy, the wiring into the bot, and the timeframe bar feed.

## OCL break rule (owner answers 2026-11-19, implemented)
- Setup is red, red, green on the last 3 closed bars.
- The first break that happens sets the key level:
  - 2-candle break: C2 closes above C1 open. Key level = C1 open. Confirmed on C2.
  - 3-candle break: C3 closes above C2 close. Key level = C2 close. Confirmed on C3.
- Conservative level = lowest body bottom of C1, C2 and C3. It applies to both breaks.
- Assumption: C3 must still be green for a 2-candle break, as the pattern requires. Confirm if the 2-candle break should fire without it.
- Tests: 18 in `tests/unit/test_owner_patterns.py`, all passing.
- Not in the test run: `src/quant/tests/test_nse_fii.py::test_market_inputs_serves_a_stored_value` fails because it uses the fixed date 2026-11-14 and now the data is stale. It is not related to this change; fix separately.

## Corrected break rule (owner, 2026-11-19; implemented)
- Engulfing: 2 candles only (unchanged): red, then green closing above the red high.
- OCL: red-red-green (3 candles). The key level's candle sets the break candle:
  - Key level C1 open: broken by C3 closing above it ("3-candle break"), checked on the 3rd candle.
  - Key level C2 close: broken by C4 closing above it ("4-candle break"), checked on the 4th candle.
  - The first break decides. If C3 already broke C1 open, the 4-candle break is not used.
- Conservative level: lowest body bottom of C1, C2 and C3.
- Assumption to confirm: the 3-candle break needs no extra condition beyond C3 green and closing above C1 open.
- Tests: 18 in `tests/unit/test_owner_patterns.py`, all passing.
- Replaces the earlier 2-candle break rule (C2 closing above C1 open).

## OCL break rule, final (owner, 2026-11-19; implemented)
- Pattern: C1 red, C2 red, C3 green.
- 3-candle break: C3 closes above C2 open. Aggressive key level = C2 open.
- 4-candle break: C4 closes above C2 open. Aggressive key level = C3 close.
- Priority: the first break decides. If C3 already broke C2 open, the 4-candle break is ignored.
- Conservative level: lowest body bottom of C1, C2 and C3.
- Replaces the previous rule (levels from C1 open and C2 close).
- Tests: 18 in `tests/unit/test_owner_patterns.py`, all passing.

## Owner rules, round 6 (2026-11-19)
- Entry key level: aggressive key level for every MTF, HTF and LTF.
- OCL extra condition: on the lower timeframe, price must SWEEP the OCL key level first. Only then does the bot look for the OCL pattern. (Sweep definition: TO CONFIRM.)
- Sell-fail: after the red-green engulfing pattern closes, wait for the next candle to TOUCH the key level. Then look for entry. (Entry rule after the touch: TO CONFIRM.)
- Naming (proposed, to avoid confusion with "engulfing"):
  - Pattern: Red-Green Engulfing, short name RGE.
  - Strategy: RGE (code id `rge`, replacing `engulfing`).
  - Sell-fail strategy: "RGE sell-fail".
  - OCL strategies: unchanged (OCL, OCL sell-fail top, OCL sell-fail bottom).
  Owner to confirm the names.

## Owner rules, round 7 (2026-11-19): five strategies and names
1. Pattern `egpattern` (red-green engulfing), strategy `Engulfing`.
2. Pattern `egpattern` used for sell-fail, strategy `Engulfing fail` (the fifth strategy).
3. OCL, OCL sell-fail top, OCL sell-fail bottom (unchanged names).

Entry flow, all strategies:
- The pattern is confirmed only when its bar closes.
- On the next candle, price touches the AGGRESSIVE key level.
- Then wait for the next timeframe to take a liquidity sweep before looking for the entry.

Still to confirm:
- Direction: the next timeframe is LOWER or HIGHER? Owner wording varies between "one lower time" and "wait for one HTF". (Earlier: LTF sweep before OCL. Latest: HTF after an MTF touch.)
- Sweep definition: wick beyond the level and back inside on the same bar, or a bar close beyond the level?

## Owner rules, round 8 (2026-11-19): liquidity sweep
- Flow: pattern confirmed on bar close, then a new candle touches the aggressive key level, then the bot looks for the liquidity sweep.
- MTF setup: sweep on HTF. The HTF swing low must be taken.
- HTF setup: sweep on LTF.
- Swing = 3-candle swing (proposed): the middle candle's low is lower than the low of the candle before and the candle after. Confirm.
- Conflict to resolve: one line says "every timeframe sweeps in one lower timeframe", but the MTF and HTF lines say MTF uses HTF and HTF uses LTF. Working assumption: MTF sweep on HTF, HTF sweep on LTF.
- Open: LTF setup sweep (on which timeframe?). Sweep definition (wick beyond and back inside, or close beyond).

## Owner answers, round 9 (2026-11-19)
- Mapping (owner did not follow the question, restated): MTF pattern sweeps on HTF; HTF pattern sweeps on LTF. Owner to confirm yes/no.
- LTF: no sweep required.
- Sweep counts when the swept level is broken by a wick OR by a body close. After the break, the OCL is formed.
- 3-candle swing: confirmed.
