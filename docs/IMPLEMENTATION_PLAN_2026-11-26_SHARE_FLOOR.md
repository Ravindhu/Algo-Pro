# Plan: lower the aggressive share floor from 50 % to 30 % (2026-11-26)

## Request (owner)
Allocate more risk to the conservative levels. Change the allowed `aggressive_share` range from 50 %-80 % to 30 %-80 %.

## What the change means
- `aggressive_share` is the split of each budget between the aggressive slots (3) and the conservative slots (2).
- Lower share = more budget goes to conservative slots.
- The range only sets which values are accepted. Budgets still split the parent budget, so total risk does not grow.
- Default stays at 0.60. Hard risk limits (loss ceilings, portfolio ceiling) are not touched.

## Phases
1. [done] Find every place that hard-codes the range (risk_tree.py SHARE_MIN, setup_store.py message, settings.yaml comment, tests).
2. [done] Change the floor to 0.30 in code, messages and comments.
3. [done] Update tests: 0.29 is now refused; 0.30 is accepted and saved; boundary tests moved to 0.30-0.80.
4. [done] Run the unit suite: 900 passed, 8 skipped.
5. [pending] Owner review of the rules doc section 11A (still says 0.50 to 0.80) in a later edit.

## Files changed
- src/quant/engines/alpha/strategies/risk_tree.py (SHARE_MIN 0.30, docstring)
- src/quant/core/setup_store.py (range message and comment)
- config/settings.yaml (comment only; value stays 0.60)
- tests/unit/test_risk_tree.py, tests/unit/test_setup_store.py (boundaries and new 0.30 test)
- docs/IMPLEMENTATION_PLAN_2026-11-26_SHARE_FLOOR.md (this file)

## Safety
Live is still off. This change alters sizing split only when the owner sets a share below 0.50.
