# Decision 0023: port the IST session clock (stage 6.5, step d, second slice)

Status: IMPLEMENTED 2026-11-06. Owner sign-off: pending.

## What changed
- New `src/quant/core/session_clock.py`: pure port of `Blue_engine/core/time_utils.py`
  (`ist_now`, `parse_hhmm`, `is_past_cutoff`, `market_session_state`, session constants).
- Tests: `tests/unit/test_session_clock.py` (hand-checked), `tests/parity/test_session_clock_parity.py`
  (grid of 7 days x 10 times x 3 cutoffs; exact match on open and state).

## Not ported, and why
- Blue's holiday-file loader (`Blue_engine/input/market_holidays.json`). The holiday list is owned by
  `quant/core/calendar.py` (`TradingCalendar`, with a `verified` flag). Two holiday sources would drift.

## Deliberate differences (fail closed)
1. No calendar, or an unverified calendar: the session is `closed_day`. Blue said open on any weekday.
2. Bad `entry_cutoff` text: the default 14:45 is used. Blue used no cutoff at all, which would allow
   new entries until 15:30.
Both are covered by unit tests. They are not in the parity grid, because Blue gives a different answer on purpose.
