# Implementation plan: margin-funded delivery (MTF), 2026-11-25

Status: PHASE 3 implemented and tested (2609 passed). Live stays off. MTF stays OFF until the owner sets
`margin.mtf_enabled: true` in config/settings.yaml and restarts the bot.

## Owner decisions (2026-11-25)
- No eligibility list. The broker decides per stock, once a day.
- Vendored OpenAlgo change approved (MTF through the order, margin and mStock paths).
- Borrowed limit = 30% of capital (`margin.mtf_borrow_pct: 0.30` x `risk_budget.capital_inr`).
- Interest = 12% a year on the borrowed amount (`margin.mtf_annual_rate: 0.12`), charged per day held.

## Daily facility reading (margin_daily.py, bot._margin_record)
- The first MTF candidate of a stock each trade day makes one broker margin call for a 1-share order at the
  reference price. The result is stored per stock for that day: available or not, and the funded share.
- Funded share = 1 - margin / full value. Available only if above 0 and at most 1 - 1/5 = 80% (5x).
- Every MTF entry in that stock that day uses the stored reading. A failed reading is stored as unavailable and is
  not retried that day. A new trade day clears the readings. An unreadable file means readings are fetched again.

## Holdings sync (margin_sync.py, bot.sync_mtf_holdings)
- Once a day, before the first MTF decision, the bot reads the broker position book (product MTF/MARGIN) and
  compares quantities per symbol with its own MTF positions. Any mismatch or read error blocks MTF entries; CNC continues.
- MTF % = borrowed (quantity x entry x funded share) / value of the MTF holdings at the broker.

## Interest (bot._accrue_mtf_interest, run at the day roll)
- Borrowed x 12% / 365 per open MTF position, booked as a realized cost. Skipped on a same-day restart so a day is never charged twice.
- Charged per trade day, not per calendar day: a weekend hold is charged once. Note this if the broker charges weekends.

## Where it lives
- `risk/margin_entry.py`: settings, limit, funded share, interest (pure).
- `risk/margin_daily.py`: the day's stored readings.
- `risk/margin_sync.py`: broker MTF holdings and reconciliation (pure).
- `execution/strategy_bot.py`: `_entry_product`, `_verify_margin`, `_margin_record`, `sync_mtf_holdings`, `_accrue_mtf_interest`.
- `execution/readonly_client.py`: `margin_for_order` (read only).
- `scripts/run/run_strategy_bot_live.py`: wires the margin reading, daily store and holdings reader.

## mStock documentation: what the docs do and do not say (checked 2026-11-25)
- Product type MARGIN is described only as "Margin Delivery". The docs do not say it is limited to MTF or to eligible stocks.
- The Modification section lists both MARGIN and MTF as product types. The docs do not explain how they differ. Our mapping
  sends MTF as MARGIN. This is unverified.
- The margin calculator examples use DELIVERY only. The response's total_charges is not defined as margin required or
  as full value. The calculator has no funding % or "margin allowed" flag.
- Conclusion: the daily reading is only as good as the calculator's answer for MARGIN. It must be confirmed on the real
  account before MTF is enabled (run one read-only reading for a small order and compare with the broker's app).

## Vendored OpenAlgo changes (local patch, NOT upstream)
The upgrade pin (`openalgo.UPSTREAM_PIN`) fingerprints only `requirements.txt`, `pyproject.toml`, `.gitignore` and `install/`.
It does not cover these files, so an upstream upgrade drops the patch silently. The guard test
`src/quant/tests/test_vendored_margin_patch.py` fails if a marker is missing. Re-apply and re-test on upgrade:
1. `restx_api/schemas.py`: OrderSchema.product and MarginPositionSchema.product accept MTF.
2. `broker/mstock/mapping/transform_data.py`: map_product_type MTF to MARGIN, refuses unknown; reverse MARGIN to MTF.
3. `broker/mstock/mapping/margin_data.py`: map_product_type MTF to MARGIN, refuses unknown.
4. `broker/mstock/mapping/order_data.py`: trade and position books map MARGIN to MTF.

## Known limits (open)
- The daily reading uses the reference price (last close). The order price can differ slightly.
- The leverage cap check uses the reading; a change in broker margin during the day is not seen until the next day.
- Holdings: positions are checked; the broker's MTF pledged holdings in the holdings book are not read.
- Account terms (pledge, forced sale) are not confirmed in writing.
- Settings and the enable flag are read at bot start: restart after any change.
- Live trading is still off. Owner-run checks are still pending.
