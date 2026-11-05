# src/quant/portfolio

Sizing and allocation. Conviction can never override a hard cap.

- `sizing.py`: fractional Kelly, wrapped from Blue. DONE.
- `tiers.py`: conviction size tiers, disabled by default (`config/conviction_tiers.yaml`). DONE.
- `rebalance.py`, `covariance.py`: wrapped from Blue. DONE.
- `optimizer.py`: HRP, kept local. Blue HRP differs (X-25). Owner decision pending.
- `allocator.py`: strategy budget. PARTIAL.
- `portfolio_risk.py`: sector, beta, stress, CVaR. PARTIAL.

Rule: final size = min(Kelly, vol target, drawdown, tier, exposure caps).
See `docs/IMPLEMENTATION_PLAN.md` (Phase 2, P2-1 to P2-3).
