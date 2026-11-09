# 0031. Money risk limits in percent of capital

Date: 2026-11-09
Status: Accepted by owner for PAPER and LIVE values. Live trading stays blocked by the Phase 3 gate.
Replaces the INR values of decisions 0015 and 0016 for the four money limits. Keeps decision 0020 (dashboard sets limits).

## Owner input (2026-11-09)
- All five limits in percent only, no rupee values in the limits file.
- Capital base: 10 lakh (INR 1,000,000), stored as `capital_inr`.
- Daily loss: -0.5% of capital (`max_daily_loss_pct: 0.005`).

## What changed
- `config/risk_limits.yaml`: `capital_inr` and the four `*_pct` keys. Rupee keys are removed.
- `src/quant/core/config.py`: `RiskLimits.from_mapping` converts each percent to rupees once at load (pct x capital_inr). The risk gate still uses the rupee fields, so gate logic is unchanged.
- A rupee key in the YAML is refused with an error. Missing capital leaves the rupee fields None, so the gate fails closed.
- `src/quant/core/limits_store.py`: dashboard saves accept only the writable names. Rupee keys are refused. A saved file is loaded again before it replaces the live file.
- `dashboard/main_dashboard.html`: rupee inputs replaced by capital and percent inputs.

## Values
| Limit | Value | Rupees at 10 lakh | Source |
|---|---|---|---|
| capital_inr | 1,000,000 | - | owner |
| max_daily_loss_pct | 0.5% | 5,000 | owner input (was 1,000 INR, so this is a LOOSER limit) |
| max_strategy_loss_pct | 0.05% | 500 | converted from 500 INR, owner to CONFIRM |
| max_order_notional_pct | 1% | 10,000 | converted from 10,000 INR, owner to CONFIRM |
| max_open_exposure_pct | 2% | 20,000 | converted from 20,000 INR, owner to CONFIRM |
| max_adv_pct | 1% of ADV | - | unchanged (already a percent) |

## Open items
1. Owner to confirm the three converted values, and that daily loss is 0.5% (a looser limit than before).
2. The dashboard's "Current limits" table still shows the derived rupee values next to the percent inputs. Read-only.
3. Parity with Blue (`tests/parity/test_money_checks_parity.py`) was not run here: it needs Blue installed.

## Verification (2026-11-09, this session)
- `pytest src/quant/tests tests/unit`: 928 passed. 2 failures, both present before this change: `test_upgrade_pin` and `test_end_to_end_blocked_while_symbol_limit_unset`.
- New tests: `tests/unit/test_percent_limits.py`. Updated: `tests/unit/test_limits_store.py`, dashboard page test, and fixtures in auto-kill, setup-store, analyzer and data-run tests (same rupee amounts, written as percent).
- Not verified: live OpenAlgo sandbox run.
