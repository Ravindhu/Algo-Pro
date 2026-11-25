# Implementation plan: BSE code to OpenAlgo symbol, checked with the broker (2026-11-25)

Flow stage: Liquidity screener -> screen file (read-only report)
Status: IN PROGRESS on branch feature/bse-code-symbol-map-2026-11-25. NOT on master.

## 1. Why
Screener.in gives some stocks as a 6-digit BSE code (e.g. 500325). The screen step
quotes them on NSE, which fails, so they are dropped. Nothing wrong is traded, but
the owner wants to know the correct symbol and whether the broker knows it.

## 2. What the code can and cannot do today
* OpenAlgo's symbol master maps BSE token -> symbol: `database.token_db_enhanced.get_symbol_info`
  (in-process; the same lazy import pattern as `engines/core/lot.py`).
* OpenAlgo's HTTP `/search` matches symbol NAMES only, not codes, so it cannot resolve a code.
* The bot trades and reads bars on NSE only. A BSE-only name therefore cannot trade yet.

## 3. Scope of this change (read-only)
* `src/quant/data/bse_symbol.py` (new):
  * `is_bse_code(text)`: exactly 6 digits.
  * `openalgo_bse_lookup(code)`: lazy import; returns the symbol or None. Any import or
    lookup error is None (fail closed).
  * `bse_report(candidates, lookup, quote_reader)`: for each 6-digit candidate:
    1. look up the OpenAlgo symbol for the code (None -> "not in OpenAlgo symbol master");
    2. quote that symbol on BSE through the read-only reader;
    3. broker match = a successful quote reply with data. Anything else = no match, with reason.
    Returns one row per code: `{code, symbol, broker_match, reason, tradable: False}`.
    `tradable` is always False in this change.
* `scripts/ops/refresh_screen.py`: after the normal run, print the BSE report for the
  dropped 6-digit candidates. The screen file contents do NOT change.

## 4. Not in this change
* Trading BSE stocks (bot bars and orders on BSE). Needs a separate plan and owner decision.
* Writing BSE rows to the screen file.

## 5. Safety
* Read only: only the existing quote call and a read of the local symbol master.
* No order, no cancel, no write to the screen file, no secret in logs.
* Fail closed: any doubt = not matched, not tradable.

## 6. Tests
* `tests/unit/test_bse_symbol.py`: code detection, resolved + matched, code not in master,
  lookup raises, quote fails, non-numeric symbols ignored, `tradable` always False.

## 7. Done criteria
* Unit suite: same results as master (only the known `test_live_switch_is_not_touched` failure).
* Branch pushed; master untouched.

## 8. Dashboard screener block (2026-11-25)
* The Screener stocks panel shows only Ticker and Price. The Name column is removed.
* Ticker is the broker's own symbol. NSE symbols pass through. A 6-digit BSE code is translated with
  `openalgo_bse_lookup` (OpenAlgo symbol master). If the translation fails, the ticker is a dash, never the number.
* The translation runs on the server in `home_view.read_screener` (`_ticker_for`). Read only.
* Caveat: the dashboard process needs the OpenAlgo symbol master to translate codes. Outside the OpenAlgo
  runtime, BSE rows show a dash until that is available.
* Tests: `src/quant/tests/test_home_view.py` (ticker translation, unknown code, lookup failure).
* Portfolio popup: the "Hold days" column is removed from the Holdings table (display only). The `hold_days`
  data is still used to pick today's open positions.
