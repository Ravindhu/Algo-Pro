# 0010. Market-impact model: keep flat 5 bps or use Blue's square-root model (DRAFT, owner to decide)

Date: 2026-11-05
Status: PROPOSED. No code change until the owner chooses an option.
Tracks: X-30 in `docs/IMPLEMENTATION_PLAN.md`.

## Facts (checked in the repo)

- Our cost path uses a flat impact of 5 bps (`src/quant/research/costs.py`).
- Our flat value is `impact_bps = 5.0` per side (`src/quant/research/costs.py`).
- Blue has a square-root impact model using `impact_coefficient` (`Blue_engine/backtest/costs.py`). It is not used by our path.
- Phase 1 exit check 3 requires impact to be included in backtest costs. A flat 5 bps meets the requirement as written; the question is whether it is realistic.
- Switching models changes backtest results, so it needs its own commit and test updates.

## Options

A. Keep the flat 5 bps. Document it as a placeholder. Add a sensitivity test that shows results at 0, 5 and 10 bps.
- Pro: simple, no behaviour change.
- Con: ignores order size relative to volume. Can understate cost for large orders.

B. Switch to Blue's square-root impact model. Calibrate `impact_coefficient` on owner data.
- Pro: impact grows with order size relative to daily volume, as market-microstructure research usually finds.
- Con: the coefficient needs calibration on real NSE data (X-10). Until then it is an estimate.

## Evidence needed before choosing

- Order size vs average daily volume for the strategies under test (needs X-10 bars).
- Realised slippage from paper or live fills (TCA, `src/quant/monitor/tca.py`, not built yet).

## Owner choice

[ ] A  [ ] B  (date, initials)
