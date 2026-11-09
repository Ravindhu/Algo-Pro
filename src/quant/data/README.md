# src/quant/data

Market data in: validation, features and market inputs.

- `qa.py`: stale and missing bar gate. A failure stops the run. PARTIAL.
- `feature_store.py`: point-in-time values only (tested in X-4). PARTIAL.
- `market_inputs.py`: NSE board-meeting event parser and blackout dates. DONE. Bulk/block deals and flow data PENDING (P2-8).
- `universe.py`, `corp_actions.py`: survivorship-free universe and corporate actions. PARTIAL.
- `ingest.py`: broker live feed. PENDING until Phase 3.

Rule: NSE data is used for event blackout only. It is not a signal.
See `docs/IMPLEMENTATION_PLAN.md` (P1-7, P2-8, P3-1).
