# Implementation plan: signal and sizing stage, end to end to the OpenAlgo bridge (paper only)

Date: 2026-11-09. Owner request: "Make work end to end till openalgo bridge".
Scope: paper path only. Nothing reaches the order API. The bridge is reached only in analyze mode
through the existing `scripts/run_paper.py --broker analyze` guard, which this change does not alter.
Live stays blocked (Phase 3 gate not met; `config/risk_limits.yaml` live values are null).

## Facts checked in the code

- `scripts/run_paper.py` already runs: signals CSV -> risk gate -> queue -> broker -> ledger.
- `src/quant/signal/` is empty. That is the missing link: bars -> strategy -> optimiser -> sizing -> signals CSV.
- Risk limits (paper minimums): 10 shares per order, INR 10,000 per order, INR 20,000 open exposure, 1% ADV.
- The gate fails closed: entries need price-band fields (prev_close, circuit_pct), liquidity (adv_shares),
  and event-blackout dates. Missing fields are rejected, not guessed.
- Known gap (not fixed here): `run_paper._intent_from_row` leaves `trade_day` as a string, so the
  builder omits it. Blackout checks then reject entries for symbols that have blackout dates (fail closed).

## Phases

- **Phase A (this doc):** inventory. Done.
- **Phase B, signal stage:** `src/quant/signal/build_signals.py`. Steps:
  1. Strategy (`MomentumStrategy`) selects symbols on the decision date, using closes up to that date only.
  2. Optimiser (`optimiser_registry.optimise`) weights the selected symbols from their covariance over a trailing window.
  3. Sizing: whole shares = floor(weight x capital / last close). Cap each order at the per-order quantity and notional limits. Stop adding entries when open exposure would pass the limit.
  4. Exits: a held symbol that is no longer selected gets a SELL exit intent for its full held quantity.
  5. Optimiser failure: raise. Nothing is written (fail closed).
- **Phase C, tests:** `src/quant/tests/test_signal_chain.py`:
  - sizing caps (quantity, notional, exposure), hand-checked;
  - intent IDs are deterministic (restart safe);
  - optimiser failure writes nothing;
  - end to end: bars -> signals CSV -> `run_paper.run()` (paper broker) -> ledger rows.
- **Phase D, verify:** run the new tests, the existing paper tests, and the unit + parity suites. Record counts.
- **Phase E, docs:** DECOUPLING/flow status for the signal stage. Note the known gaps above.

## Rules applied

- Look-ahead: the decision uses rows up to and including the decision date only.
- Sizing never raises a limit. The risk gate remains the final check.
- No live path. No change to the bridge's refusal rules.

## Work log

- 2026-11-09: Phase A done. Branch `feat/paper-signal-chain`.
- 2026-11-09: Applied the `src/quant/strategies/` part of `patches/0001-feat-phase5-...patch` (base.py, momentum.py, README, __init__). The rest of that patch (param_search, strategy_backtest, strategy_loop, trial_log change, tests) is NOT applied.
- 2026-11-09: Phases B-C done. `src/quant/signal/build_signals.py`, `src/quant/tests/test_signal_chain.py` (10 tests, all pass).
- 2026-11-09: Phase D: `pytest tests/unit tests/parity src/quant/tests`: 2096 passed, 16 skipped, 1 xfailed, 1 failed (`test_upgrade_pin`, pre-existing on master).
- Finding: with the owner's current `config/risk_limits.yaml`, every paper order is BLOCKED by `limit not set: max_symbols_per_portfolio` (fail closed). The owner must set this value. The test uses a temporary copy with a test-only value; the owner file is unchanged.
- Finding: the analyze bridge is reached; with the analyzer off, it refuses every order and never calls the order endpoint (test `test_bridge_refuses_when_analyzer_is_off`).
- Known gaps: `trade_day` is not parsed to a date in `run_paper._intent_from_row` (builder omits it); exit logic uses only the held quantities passed in; no owner data yet (bars must include prev_close, circuit_pct, adv_shares).
