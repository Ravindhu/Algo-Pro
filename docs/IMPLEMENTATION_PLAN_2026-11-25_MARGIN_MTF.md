# Implementation plan: margin-funded delivery (MTF), 2026-11-25

Status: PHASE 2 implemented and tested. Live stays off. Margin stays OFF until the owner sets
`margin.mtf_enabled: true` in config/settings.yaml, then restarts the bot.

## Owner decisions (2026-11-25)
- The mStock margin-delivery list is scrapped. Eligibility is not checked against a list.
- Vendored OpenAlgo change approved (MTF product through placeorder, the margin calculator and the mStock adapter).
- Borrowed limit = 30% of capital (`margin.mtf_borrow_pct: 0.30` x `risk_budget.capital_inr`).
- Verify leverage before firing: compare the broker's returned margin with the full value of the order.

## Rules
- Only BUY delivery entries may become MTF. Held overnight like CNC: same stop, target and trail; no MIS flatten.
- Borrowed total (open MTF positions plus unfilled MTF parents, at full notional) must stay within the limit.
- Leverage check, per new entry, before the parent is created: the broker's `total_margin_required` (from
  OpenAlgo `/api/v1/margin`, read only) must be above zero, below the full value, and imply a leverage of at
  most `margin.mtf_max_leverage` (5.0 = 80% funding, the broker maximum).
- Any failed check, a broker error, or an exception keeps the entry as CNC. The margin path never blocks a CNC entry.
- Exits and broker stops use the position's own product (`MTF` or `CNC`).

## Where it lives
- `src/quant/risk/margin_entry.py`: settings, limit choice, `verify_leverage` (pure).
- `src/quant/risk/margin_funding.py`: borrowed-limit check.
- `src/quant/execution/strategy_bot.py`: `_entry_product`, `_verify_margin`, `_mtf_borrowed_inr`, `_product_of`.
- `src/quant/execution/readonly_client.py`: `margin_for_order` (read only; the read-only guard test forbids order verbs).
- `scripts/run/run_strategy_bot_live.py`: passes the broker margin check into the bot.

## Vendored OpenAlgo changes (local patch, NOT upstream)
These edits live in `src/quant/openalgo_platform`. The upgrade pin (`openalgo.UPSTREAM_PIN`) fingerprints only
`requirements.txt`, `pyproject.toml`, `.gitignore` and `install/`, so it does NOT detect these edits and was not re-pinned.
A future upstream upgrade will drop them silently unless they are re-applied. Re-apply and re-test when upgrading:
1. `restx_api/schemas.py`: `OrderSchema.product` and `MarginPositionSchema.product` accept `MTF`.
2. `broker/mstock/mapping/transform_data.py`: `map_product_type` maps `MTF`→`MARGIN` and refuses unknown products
   (was: silent INTRADAY); `reverse_map_product_type` maps `MARGIN`→`MTF` (was: `MIS`).
3. `broker/mstock/mapping/margin_data.py`: `map_product_type` maps `MTF`→`MARGIN` and refuses unknown products.
4. `broker/mstock/mapping/order_data.py`: trade and position mapping turn `MARGIN` into `MTF`.

## Known limits (open)
- The leverage check uses the reference price (last close), not the live limit price. The order price can differ slightly.
- The leverage check runs once per new entry. A later change in broker margin is not re-checked.
- Interest in P&L: the cost model uses 12%/yr; mStock quotes 6.99–9.49%. Not changed.
- Holdings and positions sync for MTF positions from the broker: not verified against a live account.
- Account terms (pledge, forced sale, eligibility per stock) are not confirmed in writing; the broker check decides per order.
- Settings and the enable flag are read at bot start: restart after any change.
- Live trading is still off. Owner-run checks are still pending.
