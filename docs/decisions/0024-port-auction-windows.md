# Decision 0024: port call-auction window checks (stage 6.5, step d, third slice)

Status: IMPLEMENTED 2026-11-06. Owner sign-off: pending.

## What changed
- New `src/quant/core/auction_windows.py`: pure port of `Blue_engine/core/auction_windows.py`
  (`parse_auction_windows`, `current_auction_window`, `is_within_auction_window`, default window).
- Tests: `tests/unit/test_auction_windows.py` (hand-checked, plus an AST check that the module reads no
  clock, no environment variable and no Blue module), `tests/parity/test_auction_windows_parity.py`
  (parse cases, every minute 09:00 to 16:59 for 4 window specs, aware times). The Blue clock import is
  stubbed to fail if called, so Blue must use the time we pass in.

## Differences from Blue
1. No clock read. The caller passes `now`.
2. No environment read (`POV_SME_AUCTION_WINDOWS`). Windows come from config or the default.
3. No `is_in_call_auction_window` alias. It is the same function under a second name.

## Owner questions (not decided here)
1. Default window. Blue's default is one window, 09:15 to 15:30 (the whole continuous session).
   Blue's own comment describes discrete PCAS windows. The real SME/PCAS schedule is not in the repo.
   Please confirm the real schedule before live SME trading. I did not invent one.
2. Window ends. Matching is per minute, and both ends are inclusive, as in Blue. So 15:30:45 counts
   as inside a window ending 15:30. Confirm whether the end should be exclusive.
