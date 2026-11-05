# 0005: Decisions for the 15 remaining undecided modules

Date: 2026-11-04
Stages: Phase 2 and Phase 3 modules (see docs/audits/PHASE0_REUSE_AUDIT.md, step 9)
Status: accepted. Each row was compared against the Blue file read in this session.

Rule: WRAP when Blue has a function with the same inputs and outputs (checked numerically).
KEEP when Blue has no such function, or its function is stateful, has a different
policy, or is a different metric. A KEEP always names the reason.

| Module | Blue target checked | Decision | Reason |
|---|---|---|---|
| models/factors.py | ml/features/engineering.py `momentum` | **WRAP** `momentum_12_1` | Equal to `momentum(window=231, shift=21)` per column, same NaN pattern, max diff 0.0. Low-vol, z-score and composite stay local (Blue `realised_vol` is annualised, a different scale). |
| reporting/dashboard.py | backtest/metrics.py `profit_factor` | **WRAP** `profit_factor` | Same None-when-no-losses rule. Tested on 4 edge cases and against Blue. EV, win rate, MAE/MFE mean stay local (simple means). |
| risk/controls.py | core/auction_windows.py | KEEP | Blue file is PCAS call-auction time windows. Our file is index-halt freeze, event blackout and overnight gap check. No overlap. |
| execution/router.py | pov/slicers/pov.py, core/liquidity.py | KEEP | Blue child size is computed inline inside a live stateful loop (volume delta x pct). `vwap_schedule` splits a total by a profile; no Blue function does that as a pure call. |
| monitor/session.py | core/broker_auth.py | KEEP | Blue does headless TOTP login. Our class only tracks token age. Blue `login_guard` is about attempt limits, a different policy. |
| models/events/scoring.py | economics/nlp/event_calendar.py | KEEP | Blue file is an expiry and macro-event calendar, not filing-phrase scoring. |
| models/meta_label.py | alpha/confirm/state.py | KEEP | Blue ConfirmMachine is a confirmation state machine, not a take/skip meta-labeller. |
| data/ingest.py `FeedHealth` | core/quote_health.py `QuoteHealthMonitor` | KEEP | Blue counts consecutive quote failures. Ours measures heartbeat gaps. Different question. |
| data/ingest.py `normalize_fill_event` | core/order_updates.py `parse_order_update` | KEEP | Blue returns a tuple and is tied to ledger side effects. Ours returns the ledger dict and raises on bad input. |
| data/market_inputs.py | economics/providers/fii_provider.py | KEEP | Blue is a network provider. Ours reads local CSVs and has order-book imbalance and volume profile, which Blue lacks. |
| data/feature_store.py | ml/features/point_in_time.py `asof_value` | KEEP | Ours is an incremental bisect index (O(log n) lookup). Blue rebuilds a row join on every call. Same semantics, worse cost. Semantics match (latest value at or before ts). |
| data/universe.py | core/symbol_meta.py | KEEP | Blue file is restricted-segment and SME flags. Ours is listing and delisting spans for survivorship-free membership. No overlap. |
| feedback/champion_challenger.py | ml/registry/__init__.py `Registry.promote` | KEEP | Blue promotes status and re-signs. It has no human sign-off gate and no DSR-vs-champion check. Our policy is stricter and must stay here. |
| feedback/reopt.py | jobs/sector_hm_report.py | KEEP | Unrelated: a sector heat-map report job. Our file is an approval queue. Blue has no queue (same as execution/queue.py). |
| reporting/accounting.py | portfolio/tracker.py | KEEP | Blue tracks weighted-average cost. Ours is FIFO tax lots with an STCG/LTCG holding-period split. Different method. |
| notify/alerts.py | core/supervisor.py, dashboard/data_source.py | KEEP | Blue has no levelled emitter. Its `AlertEvent` is a data record. Our CRITICAL-to-handler rule is the kill-switch path. |

## Consequences

- Two src modules changed (`models/factors.py`, `reporting/dashboard.py`). Tests:
  `tests/unit/test_phase0_remaining_wraps.py` (4 tests). Unit suite: 160 passed.
- The other 13 stay local with the reasons above. Each is recorded so it is not re-checked.
- Re-check a KEEP only if the Blue file changes shape.
