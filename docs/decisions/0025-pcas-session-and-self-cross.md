# Decision 0025: PCAS session clock and self-cross guard (SME / illiquid scrips)

Status: IMPLEMENTED (Phase A) 2026-11-06. Owner sign-off: pending. Plan: docs/PCAS_AUCTION_PLAN.md.

## Why
Blue's default auction window (09:15-15:30 as one window) does not match the PCAS market. NSE
circular NSE/CMTR/23063 (26 Mar 2013) sets six 1-hour sessions from 09:30 to 15:30, with a random
closure in the last minute of order entry. A penalty applies if a client's max buy reaches its
min sell. Both rules need code.

## What changed
- `src/quant/core/pcas_schedule.py`: session clock. States: entry_open (minutes 0-43),
  random_close_zone (minute 44, unsafe), matching (minutes 45-59), outside_sessions.
  `orders_allowed()` is True only in entry_open. Fails closed on anything else.
- `src/quant/risk/self_cross.py`: `would_self_cross(side, price, resting)`. Market buy = +inf,
  market sell = -inf, so market orders are refused when they could cross an open order.
  Invalid prices raise, never become 0.
- Tests: `tests/unit/test_pcas.py` (43 cases, boundary times at 09:29:59, 09:30, 10:13:59, 10:14,
  10:15, 15:29, 15:30, and the self-cross cases).

## Not decided here (owner)
1. The 45/8/7 minute split comes from the NSE page summary, not the circular. Confirm it.
2. Which order types are allowed in PCAS, and whether market orders are allowed.
3. Closing-session rules. The circular mentions a uniform closing session, with no details.
4. Eligibility. Which SME scrips are in PCAS. The list changes each quarter.
5. Whether Blue's default auction window should be retired for PCAS use. The PCAS session clock now
   replaces it for this purpose. Blue's module is unchanged.

## Not wired yet
Neither module is connected to the gate or the POV loop. Wiring is the next step, after the owner
confirms items 1 to 4.
