# Decision 0022: port lot-size rounding for POV child orders (stage 6.5, step d, first slice)

Status: IMPLEMENTED 2026-11-06. Owner sign-off: pending (same review as 0018 to 0020).

## What changed

- New `src/quant/execution/lot.py`: pure port of `Blue_engine/core/lot.py`.
  - `lot_quantity(quantity, lot_size)`: same integer rule. Floors to a whole lot.
  - `resolve_lot_size(symbol, exchange, override=None, lookup=None)`: override first, then an injected
    `lookup(symbol, exchange)`, then `None`. Bad values and lookup errors give `None`, never `0`.
- Tests: `tests/unit/test_lot.py` (hand-checked), `tests/parity/test_lot_parity.py` (Blue grid, exact match).

## What was not ported, and why

- Blue's master-contract DB lookup (`database.token_db_enhanced`) and SDK lookup (`client.symbol()`).
  They are broker and data adapters. The execution package must not import them.
  The caller now passes a `lookup` function. A data-layer adapter will supply it later.
- The SDK path is a no-op for the current broker adapter, so nothing is lost today.

## Why it matters

Child orders must respect exchange lot sizes. A child of 2,500 against a 1,200 lot becomes 2,400.
A missing lot size must not turn into 0 or into a guessed value. Here it becomes `None`, and the
caller must decide what to do.

## Still open in step d

- `core/time_utils.py`, `core/auction_windows.py`, `core/symbol_meta.py`, `core/benchmark.py`,
  `core/liquidity.py`, `core/band.py`, `pov/slicers/guards.py`, `pov/slicers/pov.py` (the loop).
- The POV loop takes the order port (`PovExecutor`) and the read port (`OpenAlgoReadOnly`) as inputs.
