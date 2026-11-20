# The five strategies and the full trade flow, in plain English (updated 2026-11-20)

This replaces the 2026-11-18 version, which described the old stops and targets. The bot now works as described here.
The step-by-step code map is in `docs/STRATEGY_RULES_2026-11-19.md`, section 15. Known open problems are in section 15.8.

All five are long only (BUY). The bot refuses SELL signals.

## Words used

- **MTF, HTF, LTF**: the three timeframes from the Trade setup panel. Order must be LTF < HTF < MTF. Example: Daily, 1-hour, 5-minute.
- **Candle body**: the part between open and close. A "50 % body" means the body is more than half of the candle's full range.
- **OCL**: three candles, red, red, green (C1, C2, C3). A break is either C3 closing above C2's open (3-candle break) or C4 closing above C2's open after C3 did not (4-candle break). The first break wins.
- **Aggressive level**: the first price level the bot may buy at.
- **Conservative level**: a second, deeper level, touched later. It is the lowest candle body, not the lowest wick.
- **Sweep**: a later bar's low goes below an earlier swing low.
- **HTF L**: the lowest low of the HTF OCL. This is the one hard stop for the whole cycle.
- **ATR**: how far price usually moves in one bar (14-bar average).
- **POV**: the share of the traded volume the bot is allowed to take in each slice.

## The five strategies

| Bot name | MTF setup | Aggressive level | Conservative level | HTF sweep first? |
|---|---|---|---|---|
| `engulfing` | Bullish engulfing (two candles) | Open of the first candle | Lowest body of the two candles | Yes |
| `engulfing_fail` | Sell-fail engulfing | Highest body top of the two candles | Lowest body of the two candles | Yes |
| `ocl_seq` | OCL | The OCL's key level (C2 open, or C3 close for a 4-candle break) | The lowest body of C1 to C3 | Yes |
| `ocl_sell_fail_top_seq` | OCL | Highest body top of the OCL | Open of the candle before the trigger | Yes |
| `ocl_sell_fail_bottom_seq` | OCL | Lowest body bottom of the OCL | None (one level only) | No |

The bot runs all five. Each one is separate and does not depend on the others.

## The flow, from MTF to the POV slicer

This runs every bot cycle, every 5 minutes. The replay reads the whole history each time, so nothing is carried between cycles except the open positions and the carry pool.

**Stage 1: check the inputs**
1. Read the screen. Use only symbols from a screen at most 10 minutes old, with no ASM or GSM flag.
2. Work out each symbol's allocation from the portfolio engine. The total is 5 % of equity across all symbols, with at most 4 % for one symbol. One symbol gets 4 %, two get 2.5 % each, five get 1 % each.
3. Read closed MTF, HTF and LTF bars. If a feed is missing, skip that symbol.
4. If entries are blocked (for example, startup checks not passed), no new setups are looked at. Exits and open positions are still handled.

**Stage 2: find the setup**
5. Find the newest confirmed MTF pattern for the strategy. This gives the MTF level.
6. Wait for a later MTF bar to touch that level. That is the MTF touch.
7. If a later MTF bar closes below the pattern low, the whole setup is over.
8. If a later MTF bar touches the deeper (conservative) level, that counts as the MTF conservative touch. It is recorded for the budget.
9. For all strategies except `ocl_sell_fail_bottom_seq`: after the MTF touch, wait for an HTF swing low to be swept. The HTF replay starts from that sweep.
10. After the start, find the first HTF OCL. Wait for a later HTF bar to touch its key level. That is the HTF key touch. The HTF OCL's low becomes HTF L.
11. A later HTF bar may touch the HTF conservative level. That is the HTF conservative touch.
12. If an HTF candle closes below HTF L, the HTF cycle ends. The next HTF OCL must form below that price. No new sweep is needed.

**Stage 3: the LTF loop, inside each HTF cycle**
13. After the HTF key touch, find the first LTF OCL. Its aggressive level is the entry level.
14. An LTF bar touches the aggressive level: an aggressive touch.
15. Only after that, a later LTF bar may touch the same OCL's conservative level: a conservative touch. From then on, the trade exits by trail, not by a fixed target.
16. If an LTF candle closes below the OCL's low, the OCL is invalidated. Its unfilled shares are paused, not closed. The next LTF OCL must form below that low.
17. If the HTF cycle is invalidated, the whole HTF cycle ends and the loop starts again on the next HTF OCL (step 12).

**Stage 4: decide the budget (the risk tree)**
18. Every touch gets a budget from the tree. The numbers below assume 4 % for one symbol and 60 % aggressive:
    - **MTF level:** each new HTF OCL uses a 0.8 % slot. There are five slots: three aggressive, then wait for the MTF conservative touch, then two conservative.
    - **HTF level:** each new LTF OCL inside an HTF OCL uses a 0.16 % slot. Five slots: three aggressive, then wait for the HTF conservative touch, then two conservative.
    - **LTF level:** each LTF OCL's 0.16 % is split 60/40. The aggressive touch gets 0.096 %, the conservative touch gets 0.064 %.
19. A conservative touch unlocks the next slot. The next OCL to form uses it. Aggressive slots need no unlock.
20. Unused slot budget is lost. Shares that were not filled move on to the next OCL, but carried shares add no new budget.
21. A touch with no budget (its slot is not open) is refused. Nothing is sent.

**Stage 5: size the order**
22. Shares = equity × budget ÷ sizing stop, rounded down, plus any carried shares.
23. Sizing stop = the larger of 1 × ATR(14) and (entry price − HTF L + 0.05 % of entry price). So a very tight stop can never give a huge position.
24. Checks run in this order, and any failure cuts the order down or refuses it:
    - the portfolio cap (5 % of equity of planned risk across all open cycles),
    - the per-order share and value caps,
    - the liquidity cap (share of average daily volume),
    - the risk gate.
    A cut-down amount is carried to the next OCL and logged.
25. The order goes to a parent for this HTF cycle. The parent's stop is HTF L. Its allocation is locked when the cycle opens, so a later screen change does not change it.
26. If the HTF cycle closes with unfilled shares, they go into a pool for the same MTF setup. The pool is used only by the next order in that setup. If the setup ends first, the pool is dropped.

**Stage 6: slice the order (POV slicer)**
27. Each cycle, the slicer works the oldest order that still has unfilled shares. A parent that is paused (after an LTF invalidation) is not sliced.
28. The slice rate depends on the buyer-initiated VWAP for the day, built from live quote polls:
    - Price above VWAP + 0.5 %: **halt**. No slice this cycle.
    - Price below VWAP − 3 %: **passthrough** at 10 %.
    - In between: the rate moves in a straight line from 5 % (near the top) to 25 % (near the bottom).
    - No VWAP yet: fixed 10 %. The log says so.
29. The slice is based on the last closed bar's volume times today's buyer share of volume. If the buyer share is not known yet, the full bar volume is used.
30. The child quantity is the rate × volume, capped at the parent's remaining shares and the order's unfilled shares. Less than 1 share: nothing is sent.
31. In the default mode, the entry is a LIMIT at the best bid plus one tick, capped at the reference price. It waits 5 seconds and is tried at most 3 times. An unfilled LIMIT is cancelled and the parent stays open.
32. Exits are never sliced by VWAP. They use the fixed 10 %.
33. If the broker returns a partial fill, the bot books the shares that filled and keeps the rest of the order open. If the rest could not be cancelled, the bot raises a CRITICAL alert and blocks entries until someone checks the broker.

**Stage 7: manage the open position (each cycle, on the last closed bar)**
34. The stop is checked first. If the bar's low reaches the stop, the position exits at the stop (or the open, if the open is lower).
35. Before any conservative touch, the position may exit at its target (average + 2.1 × R, where R = average − HTF L). After a conservative touch there is no fixed target.
36. Breakeven is off for these positions.
37. After a conservative touch, the stop trails: best high since entry minus 2 × ATR. It only moves up.
38. Every stop move is sent to the broker as a stop order.

## What is still open

- Unfilled shares are dropped when the MTF setup fails (owner confirmed 2026-11-20).
- Profit lock before a conservative touch: none yet. The only protection is HTF L. This needs an owner decision (rules doc 15.8, D).
- The partial-fill handling is coded but not yet tested on the paper broker.

The replay's exit mode and target now come from the tree's own sizes, so they no longer depend on the old 1 % loss ceiling.
