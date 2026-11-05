# src/quant/research

Research validation: prove an idea is real before it is used.

- `costs.py`: Indian cost model (STT, brokerage, GST, stamp duty, SEBI fee). Rule tests DONE. Exchange charge rate still open (X-5).
- `backtest.py`, `walk_forward.py`: costs included, purged walk-forward. PARTIAL. Needs real data.
- `trial_log.py`: every run logged. Needed before any evidence counts (X-12).
- `validation.py`: DSR and PBO, computed locally. Do not use Blue's `deflated_sharpe` (X-26).
- `lineage.py`: experiment lineage. PARTIAL.
- `capacity.py`: max capital per strategy. PENDING.

See `docs/IMPLEMENTATION_PLAN.md` (Phase 1, X-5, X-10, X-12).
