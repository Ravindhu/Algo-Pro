# Implementation plan: choose NSE or BSE by best quote, place orders there (2026-11-25)

Flow stage: Execution routing (live order path)
Status: step 1 DONE on branch feature/exchange-router-logic-2026-11-25: pure choice logic
(src/quant/execution/exchange_router.py, 16 tests). Not wired into orders yet.
Owner confirmed 2026-11-25: existing live approval is enough; long exit = sell only.
Live gate: unchanged (approval file and risk gate still required).

## 1. Owner decisions (2026-11-25)
1. **Exchange rule.** NSE is the default when the stock lists on NSE. BSE is used only when
   BSE is at least 0.05% cheaper than NSE. A BSE-only stock is traded on BSE only.
2. **No liquidity skip.** Both exchanges are checked and the order goes to the chosen one.
   No minimum depth or volume rule.
3. **Exits.** Exits are placed at the best bid on the exchange the position is held on.
4. **No paper-first stage** (owner choice). Live routing is not tested in paper mode first.
   The existing live approval file and risk gate are NOT removed by this plan.

## 2. Rules derived from the decisions (assumptions to confirm)
* **Entry, buy.** Use the lower ask. BSE wins only if
  `bse_ask <= nse_ask * (1 - 0.0005)`. Otherwise NSE.
* **Entry, sell (short, if the strategy allows).** Use the higher bid. BSE wins only if
  `bse_bid >= nse_bid * (1 + 0.0005)`. Otherwise NSE.
* **Exit.** Place at the best bid on the exchange the position is held on (assumed: exits
  for a long are sells at best bid). Exits never move to the other exchange.
* **Missing quote** on one exchange: use the other exchange if it has a valid quote,
  otherwise do not trade (fail closed).
* **Crossed or zero quotes:** do not trade.

## 3. Work still needed (not started)
* Carry the exchange on every intent, order, stop, and open position (today the bot assumes NSE).
* Read bars on the chosen exchange; BSE-only symbols need BSE bars.
* Circuit band, lot size and cost model per exchange.
* Kill switch, daily loss limit and stop placement unchanged in rule; applied per exchange.

## 4. Safety requirements before any live BSE order
* Unit tests for the exchange choice (0.05% boundary, missing quote, crossed quote, exit path).
* Owner review of the diff. A dry run of the choice logic on real quotes (no order) before go-live.
* The existing live approval step stays on. Owner confirmation is still needed before live
  routing is enabled, as the live gate requires.

## 5. Open questions for the owner
* Exit rule assumed: sell at best bid for long exits. Confirm.
* Confirm that "no paper-first" means live routing goes straight to real orders once the
  code is reviewed and the live gate is approved.
