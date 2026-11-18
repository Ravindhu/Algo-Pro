# 0037. Cost-model units and null-cap semantics (audit 2026-11-17)

Date: 2026-11-18
Status: APPLIED. Every rule below is implemented and tested in
`src/quant/tests/test_audit_2026_11_17_fixes.py`.
Tracks: items 1, 2, 3, 4, 6, 7, 9, 10, 11 and the three smaller items in
`docs/algo-pro-audit-2026-11-17.md`.

The audit left two questions for the owner. Both are answered below with the
option that fails closed, because either answer changes how money is charged or
when an entry is allowed. **Changing either is an owner decision.**

## 1. Brokerage: flat per order, or percent? Delivery or intraday?

**Answer: both forms are now supported, and the default stays a percent.**

`config/costs` may set either:

| Key | Meaning |
|---|---|
| `brokerage` | bps of notional per side (default 3.0) |
| `flat_brokerage_per_order` | rupees per executed order |

When `flat_brokerage_per_order` is set it **replaces** `brokerage`. A broker
charges one or the other; charging both would over-state cost on every ticket.
The fee is charged in **rupees**, not converted to bps, because a flat fee does
not scale with ticket size — that is the whole point of item 2. A Rs 20 fee is
100 bps on a Rs 2,000 order and 1 bp on a Rs 2,00,000 order, and a single bps
number cannot express both.

**STT is product-dependent and must be set per run:**

| Product | Buy | Sell | Config |
|---|---|---|---|
| Delivery (default) | 0.1% | 0.1% | `stt: 10.0` |
| Intraday | none | 0.025% | `stt: 2.5` |

The intraday setting over-charges the buy leg by 2.5 bps because the model has
one STT rate for both sides. That is stated in the `CostModel` docstring rather
than hidden. It is conservative (over-states cost), which is the safe error for
a backtest that decides whether a strategy survives costs.

## 2. Null cap: reject all entries, or switch only that check off?

**Answer: one rule for every cap — null is NOT SET, and an unset cap REFUSES.**

This is what the code already did; only the documentation contradicted itself.

- `owner_limits.apply_owner_limits` adds nothing for a null cap. It never
  invents a threshold and never removes one.
- `risk_gate` refuses a **live** intent that has no monetary cap
  (`require_live_ack`, default true). A share-count cap is not accepted as the
  acknowledgement, because 10 shares of a Rs 50 stock and of a Rs 50,000 stock
  pass it identically.
- `portfolio.risk_limits.portfolio_risk_checks` refuses a null sector or beta
  cap (`"limit not set: max_sector_pct"`).
- `core.config.RiskLimits.unset()` reports the null book caps, and the live
  startup gate refuses to start while any is unset.

No path silently disables a check because a value is null. The header of
`config/risk_limits.yaml` said "null rejects entries" while the book-cap block
said "null switches its check off". Both now say the same thing, and that thing
matches the code.

## What changed in code, item by item

| Item | Change |
|---|---|
| 1 | `slippage` is ONE setting (`cost_aware.DEFAULT_SLIPPAGE_BPS`). `CostModel` reads it and charges it once, in the fill price. The planner adds it back through `cost_aware.planner_costs`. Neither double-charges it. |
| 2 | Flat fee in rupees, replaces bps brokerage; STT overridable per product. |
| 3 | `profit_factor` is NET, so it agrees with win rate and average trade. Gross is still reported as `gross_profit_factor`. |
| 4 | Null semantics stated once and enforced by tests. |
| 6 | `observed_slippage_pct` needs a declared `slippage_unit` (`fraction` or `percent`). An undeclared unit vetoes. |
| 7 | `veto_reason` is no longer truncated to 15 words. |
| 9 | `SlippageReconciler` measures SELL fills too, signed against the side traded, and stays floored at zero. |
| 10 | `slippage_penalised_rate` caps `penalty` at 1.0, so it can never raise the rate. |
| 11 | `record_child` writes `avg_price` to the child it claimed, not the list tail. |
| small 1 | An open position is marked with its hypothetical exit cost. |
| small 2 | The Sortino docstring now describes the code (full-sample semi-deviation). |
| small 3 | A SELL fills at `reference * (1 - slip)`, the same bps the BUY pays. |

## Not changed

- The SELL-leg STT asymmetry for intraday (stated, conservative).
- Whether `flat_brokerage_per_order` is the right default for the owner's
  broker. **Owner input needed**: set it in the run's cost config.
