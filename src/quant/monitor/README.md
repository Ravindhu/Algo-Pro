# src/quant/monitor

Post-trade: what really happened, and whether the system is still healthy.

- `ledger.py`: intent ID trail and restart reconciliation. DONE.
- `safe_mode.py`: safe-mode controller. PARTIAL (reason rule tested).
- `live_monitor.py`, `strategy_health.py`: drawdown, rolling Sharpe, CUSUM auto-disable. PENDING.
- `tca.py`: slippage vs arrival price, feeds back to the cost model. PENDING.
- `parity.py`: live paper fills vs replayed backtest. PENDING (P3-7).
- `drift.py`, `throttle.py`, `session.py`: PENDING.

See `docs/IMPLEMENTATION_PLAN.md` (Phase 3, P3-4, P3-7).
