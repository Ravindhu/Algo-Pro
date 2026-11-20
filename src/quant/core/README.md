# src/quant/core

Shared basics used by every other folder.

- `calendar.py`: trading calendar loader. 2026 NSE holidays are filled (`config/calendar/holidays.csv`). Expiry-day rules are NOT done (X-1).
- `config.py`, `types.py`: config loader and shared types.
- `blue_bridge.py`: the place where `src/quant` loads code from `Blue_engine/`.

Status: calendar DONE for holidays; expiry and lot size PENDING.
See `IMPLEMENTATION_PLAN.md (deleted 2026-11-20, in git history)` (P1-1, X-1).
