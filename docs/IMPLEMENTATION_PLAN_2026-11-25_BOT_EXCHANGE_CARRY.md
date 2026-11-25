# Implementation plan: carry the exchange through the bot (2026-11-25)

Flow stage: Execution, bars, positions, risk (live order path)
Status: PLAN ONLY on branch feature/bot-exchange-carry-2026-11-25. No code changed yet.
Live gate: unchanged. Each slice is reviewed by the owner before it is merged.

## 1. What the code does today (found by search on master 33f79f8)
* `core/types.py` `TradeIntent`: no exchange field. The gate and every intent assume one exchange.
* `execution/openalgo_client.py`: the exchange is set ONCE when the client is built
  (`OpenAlgoClient(exchange="NSE")`). `place`, `cancel` and stop calls use that single value.
* `execution/strategy_bot.py`: positions and parents are keyed by symbol only. Bars come from one
  `self._bars` object, which is one exchange. `read_screen` returns symbols only, with no exchange.
* `scripts/run/run_strategy_bot_live.py`: the VWAP book, the depth and quote calls for the peg
  verifier, and the bar feed all pass "NSE" literally.
* `data/live_bars.py`, `data/mtf_bars.py`: one exchange per instance (default NSE).
* `engines/core/lot.py`, `data/circuit_band.py`, `risk/gate.py`, `engines/core/paper_broker.py`:
  lot size, band and gate checks assume NSE.
* Grep count: 26 hardcoded "NSE" values across 16 files in execution, data, risk and engines.

## 2. Target design
* `TradeIntent.exchange: str = "NSE"` (default keeps today's behaviour).
* The entry price and exchange come from `exchange_router.choose_entry` using quotes from BOTH
  exchanges at the moment of the entry decision (not from the screen file).
* Positions and stops are keyed by `(symbol, exchange)`.
* Exits use `exchange_router.exit_price` on the exchange the position is held on.
* Bars are read per exchange (a map of exchange -> bar reader).
* Lot size, circuit band and cost model are looked up per exchange.

## 3. Slices (each one separately reviewed, tested and merged)
1. **Types and client (no behaviour change).** Add `TradeIntent.exchange` (default NSE). The order
   client uses `intent.exchange` per call. Tests: default NSE; BSE intent sends exchange BSE;
   unknown exchange refused.
2. **Entry choice.** In the bot, before an entry, read quotes for both exchanges, call
   `choose_entry`, and set the intent's exchange and price. Screen file unchanged.
   Tests: BSE-only stock gets a BSE intent; 0.05% boundary; no-quote refusal.
3. **Positions, stops, exits.** Key positions and stops by (symbol, exchange). Exits at best
   bid on the held exchange only. Tests: no exit switches exchange; stop on the right exchange.
4. **Bars and feed.** Bar reader per exchange; peg verifier and VWAP book take an exchange.
   Tests: BSE-only symbol reads BSE bars.
5. **Lot, circuit band, cost.** Per-exchange lookups in lot, circuit band, risk gate and cost model.
   Tests: BSE band and lot used for BSE intents.
6. **Dry run (no orders).** Run the full decision on real quotes for the screen list. Owner reviews the
   output before any live BSE order.

## 4. Stop conditions
* Any slice that changes NSE behaviour fails its existing tests -> stop and fix before continuing.
* Any missing quote, band or lot value on the chosen exchange -> do not trade (fail closed).

## 5. Owner input needed
* Approve the slice order above.
* Confirm that slice 6 (dry run on real quotes) must be reviewed before live BSE orders are allowed.

## 6. Slice status
* Slice 6 (dry run): IMPLEMENTED. `src/quant/execution/exchange_dry_run.py` (pure decision, read-only
  reads) and `scripts/ops/dry_run_exchange.py` (prints one row per screen symbol: listing, buy exchange
  and price, exit exchange and bid, lot size; writes nothing). Tests: `tests/unit/test_exchange_dry_run.py`
  (10 tests, including a check that no order code is referenced). Cost rule unchanged for both exchanges.
  The BSE entry refusal in `_gate_entry` remains in force; lifting it is an owner decision.
  Run after the broker login is renewed: `python scripts/ops/dry_run_exchange.py`.
