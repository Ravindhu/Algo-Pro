# PCAS (periodic call auction) plan for SME and illiquid scrips

Plain English. Trading in an auction session means orders are collected, then matched in one batch.
The rules differ from normal continuous trading, so the system must know the session clock.

## Verified facts (sources checked 2026-11-06)

Primary circular: NSE/CMTR/23063 dated 26 March 2013
(https://nsearchives.nseindia.com/web/sites/default/files/inline-files/CMTR23063.pdf)
- Six one-hour sessions: 09:30-10:30, 10:30-11:30, 11:30-12:30, 12:30-13:30, 13:30-14:30, 14:30-15:30 (IST).
- Each session has an order-collection period, then a matching period.
- System-driven random closure in the last minute of order entry, between the 44th and 45th minute.
- Maximum price band 20%. Bands may be tightened by surveillance.

NSE PCAS page (summary read 2026-11-06):
(https://www.nseindia.com/products-services/equity-market-periodic-call-auction)
- Each hour: 45 min order entry, 8 min matching and confirmation, 7 min buffer.
- Penalty if a client's max buy price is at or above its min sell price and the orders trade:
  the higher of 1% of trade value (0.5% per side) or Rs 5,000 per client per session (Rs 2,500 per side).
- Circuit-breaker trigger cancels the session and purges orders.
- Covers illiquid scrips. The page does not mention SME.

## Not verified (must be confirmed before live use)

1. Which order types are allowed in PCAS, and whether market orders are allowed.
2. The closing-session rules (a uniform closing session across exchanges is mentioned, no details).
3. Whether a given SME scrip is in PCAS at all. Eligibility changes each quarter.
4. Exact matching and buffer timings (45/8/7 is from the page summary, not the circular).

## What the system will do

Phase A (this step, pure logic, tested):
- `src/quant/core/pcas_schedule.py`: session clock. States: entry_open, random_close_zone,
  matching, outside_sessions. New orders allowed only in entry_open, before the 44th minute.
- Self-cross guard `src/quant/risk/self_cross.py`: refuse a buy at or above an own resting sell
  (the penalty condition). A market order is refused when an opposite resting order exists,
  because its price is unknown.

Phase B (later, needs owner answers above):
- Wire the schedule into the gate (as an entry blocker for PCAS symbols) and into the POV loop.
- Eligibility loader for the quarterly illiquid list (input data, not decided by the code).
- Circuit-breaker and session-purge handling in order reconciliation.
- Closing-session handling once its rules are confirmed.

## Replaces

Blue's default auction window (09:15-15:30 as one window, `core/auction_windows.py`) is not
the PCAS schedule. Phase A does not change Blue's module; the PCAS schedule is a separate module.
Decision 0024 asked the owner to confirm the schedule. The circular answers the session timings.
