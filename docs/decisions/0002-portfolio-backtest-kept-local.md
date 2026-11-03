# 0002: Keep the portfolio replay in `src/quant/research/backtest.py`

Date: 2026-11-03
Stage: Historical bars, Replay simulator (Phase 1)
Status: accepted for Phase 1. The owner should confirm (see "Question for the owner").

## Context

The header of `research/backtest.py` names `Blue_engine/backtest/engine.py` and
`backtest/loops.py` as wrap targets. Checked against them:

| | `research/backtest.py` (src) | `backtest/engine.py::run_backtest` (Blue) |
|---|---|---|
| Instruments | Many symbols at once | One `symbol` per run |
| Input | Bars table + `target_qty` table, absolute shares | OHLCV rows + a strategy callable |
| Capital | One shared cash balance across all symbols | One account per run |
| Sizing | Caller sets absolute quantity | Strategy returns an exposure fraction in `[-max, +max]` |
| Look-ahead guard | Target set at close(t) fills at open(t+1), by construction | Strategy sees only the prefix up to the decision bar |
| Costs | `CostModel` (now a wrap of Blue's cost model) | Blue's cost model |
| Output | equity series, trades, `total_cost`, `missed_fills` | `BacktestResult`, fills, envelope |

`backtest/loops.py` is not a backtest engine. It holds `ReoptimizationSignal`,
`LiveMonitor`, `ReconciliationResult`, and `SlippageReconciler`. Those belong to the
monitor and TCA work (`monitor/tca.py`, `monitor/live_monitor.py`), not to this file.

## Why a wrapper does not fit

Blue's engine has no multi-symbol cash model. Running it once per symbol gives each
symbol its own starting capital. That overstates leverage and breaks the cap in
`config/risk_limits.yaml`. Translating the target table into a strategy callable would
also need a new adapter, and the result would still be one account per run.

Keeping the replay local is the smaller risk. It is portfolio-level, and it reuses
Blue's cost model through `research/costs.py`.

## Decision

- Keep `run_backtest` local. Its cost step already uses Blue's model (wrap).
- Do not expose it as a general backtest engine. It is the Phase 1 replay only.
- Keep `loops.py` as a reference for the TCA step. Do not wrap it here.

## Question for the owner

Should the Phase 1 replay be extended to match Blue's engine, or should Blue's engine gain
a portfolio mode? Both are multi-day changes. Until you decide, Phase 1 uses this local replay.
