# The four strategies, in plain English (2026-11-18)

Use this to explain how each strategy differs. The four are:
1. Engulfing
2. OCL
3. Sell-fail OCL, top
4. Sell-fail OCL, bottom

All four are long-only (BUY). A SELL signal is refused by the bot (owner rule, 2026-11-09).

## Words used below

- **MTF, HTF, LTF**: the three timeframes from the Trade setup panel. MTF is the setup timeframe, HTF is the trend/bias timeframe, LTF is the entry timeframe.
- **SL**: stop loss. The price where the trade is closed to limit a loss.
- **TP**: take profit. The price where the trade is closed to lock a gain.
- **Breakeven**: the stop is moved up to the entry price, so a winning trade cannot turn into a loss.
- **Trailing**: the stop follows the price up, so profit is protected as the price rises.
- **R**: the risk on the trade, meaning the distance from entry to the first stop. 1R is one unit of risk.
- **ATR**: a measure of how much the price usually moves in one bar.

## Side-by-side

| | Engulfing | OCL | Sell-fail OCL, top | Sell-fail OCL, bottom |
|---|---|---|---|---|
| Idea | Bullish engulfing candle, then price retraces to a Fibonacci level inside it | Red-Red-Green pattern on the higher timeframe, then a touch of a key level on the lower timeframe | A short setup that fails, so price comes back up through the level and we buy the retest | Same failed-short idea, but only after the top level has already failed. Uses the lowest swing candle |
| Timeframes used | MTF only (its signal bar) | HTF for the pattern, LTF for the entry touch | HTF for the pattern, LTF for the entry touch | HTF for the pattern, LTF for the entry touch |
| Entry | Price touches the fib level (88.6% of the engulfing candle) within 5 bars of the pattern | Price touches the trigger level (open of candle 3, conservative mode) and the LTF pattern confirms | First: broken candle-3 open (aggressive). If that fails: candle-2 open (conservative) | Single level: open of the lowest swing candle |
| Stop loss | 1.5 × ATR below entry | HTF stop: the low of the HTF pattern. LTF stop: the entry's own stop | Same two-tier stop as OCL | Stop is the low of the lowest swing candle |
| Take profit | 3 × ATR above entry (fixed) | 3R by default (target_rr = 3.0), in FIXED exit mode only | Same as OCL: 3R in FIXED mode, none in DYNAMIC mode | Same as OCL |
| Exit mode | Fixed TP and SL | AUTO: FIXED if entry is at the aggressive level, DYNAMIC if at the conservative level | Same as OCL | Same as OCL |
| Breakeven (inside the strategy) | Not set in the strategy | At 1.5R, stop moves to entry plus costs | Same as OCL | Same as OCL |
| Trailing (inside the strategy) | Not set in the strategy | 2 × ATR, measured from the highest close, only moves up | Same as OCL | Same as OCL |
| Retries | Setup is voided if touched early | Up to 3 LTF stop-outs per pattern, each re-entry lower | Same, and can fall through to the bottom strategy | Up to 3 LTF stop-outs |

## How the bot actually exits (important)

The bot does not use the strategy's own breakeven and trailing rules as the final word. It uses its exit manager, with these defaults from `config/bot.yaml`:

- **Stop and target**: the values the strategy hands over (SL and TP) are used.
- **Breakeven**: at 1R, the stop moves to entry. The OCL classes say 1.5R, but the bot uses 1R unless the strategy has an override under `exits:` in `bot.yaml`.
- **Trailing**: 2 × ATR, from the best price seen since entry. Only moves the stop up.
- **Tie rule**: if one bar touches both the stop and the target, the stop wins.

So for all four strategies today, breakeven and trailing are the bot's 1R and 2 × ATR, unless `bot.yaml` is changed. The per-strategy breakeven and trailing inside the OCL code are not used by the bot yet. Confirm this with the code before you rely on it in a live decision.

## The timeframe reality right now

The bot reads one set of 5-minute bars for every strategy.

- Engulfing runs on 5-minute bars, so its MTF value only counts once the bot can load bars at that interval.
- OCL and both sell-fail strategies would read 5-minute bars as HTF and LTF, which is wrong. Until the multi-timeframe bar feed is built, the bot must refuse any setup that needs a different interval.

## One-line summary for each

- **Engulfing**: one candle pattern on one timeframe, with fixed stop and target based on ATR.
- **OCL**: a higher-timeframe pattern, then a lower-timeframe entry, with a 3R target and a two-tier stop.
- **Sell-fail top**: a failed short, then buy the retest. Two entry levels; if the first fails, use the second.
- **Sell-fail bottom**: the same failed short, used only after the top level has failed. One level at the lowest swing candle.
