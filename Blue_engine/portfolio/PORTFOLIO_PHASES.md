# Portfolio Module — Phase Plan & Work Log

Companion to `PORTFOLIO_ENGINE_PLAN.md` (the engine/allocation milestones M1–M10).
That document covers *building the allocator*. **This one covers the portfolio
MANAGEMENT layer** — accounting, risk, attribution, optimisation and the ML/AI
overlay — i.e. what a portfolio manager needs on top of a working allocator.

Status: `[x]` done · `[~]` in progress · `[ ]` pending

---

## Phase 1 — Accounting core (equity curve) — ✅ DONE

> **Why first:** every metric in Phases 2–5 is computed from one artefact, a
> trustworthy equity curve. Positions alone cannot produce returns.

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | `EquityTracker` — weighted-average cost basis, fills, NAV | [x] | `portfolio/tracker.py` |
| 2 | India charge model applied **per side** (STT/brokerage/GST/stamp) | [x] | `tracker.py` (`charges_bps`) |
| 3 | IST day boundary (not UTC) for the trading date | [x] | `tracker.ist_date()` |
| 4 | NAV persistence → `data/portfolio_nav/YYYY-MM.jsonl` | [x] | `tracker.store_nav()` |
| 5 | `daily_returns()` — NAV path → return series | [x] | `tracker.daily_returns()` |
| 6 | Manager wiring: `ingest_trades` / `snapshot_nav` / `get_equity_curve` | [x] | `portfolio_manager.py` |
| 7 | Post-close job freezes the daily equity row | [x] | `jobs/daily_portfolio.py` |
| 8 | Tests (29) — cost basis, charges, IST, round-trip, degradation | [x] | `tests/test_tracker.py`, `tests/test_manager_equity.py` |

### Design decisions worth remembering

* **Weighted-average cost, not FIFO.** Indian brokers (Zerodha/Upstox/…) report
  average cost in the contract note, so matching that convention means the
  engine's P&L reconciles with the broker's instead of contradicting it.
* **Charges are per side.** A round trip pays twice. The docstring example makes
  this explicit: a plausible +5000 gross on 10 lakh becomes **+4466 net** at
  12 bps/side — 10.7% of the edge gone. Any strategy with a few-bps edge per
  trade inverts sign once this is applied.
* **Idempotent ingestion by CONTENT, not by count.** A positional cursor breaks
  when the caller sends a single new fill (instead of a growing window) and
  double-books when a sliding window repeats. `_fill_key()` hashes
  `(symbol, exchange, side, qty, price, ts)`, so re-sending any overlap is safe.
* **Two separate records, deliberately.** `portfolio_runs` = allocation history
  (what we *wanted* to hold); `portfolio_nav` = equity history (what it was
  *worth*). Merging them would create two definitions of "return".
* **Bookkeeping is never fatal.** Every I/O path returns `None` and logs on
  failure — same rule as `core/ledger.py`.

### Bugs found and fixed while building Phase 1

1. **Falsy-zero config bug (real, in production code).** `charges_bps=0.0` was
   silently replaced by the 12 bps default because `x or DEFAULT` treats `0.0`
   as missing. A paper-trading account configuring zero charges would have had
   charges applied. Fixed with explicit `is None` checks; there is now a test.
2. **Docstring arithmetic wrong (my error).** I wrote the net as 4997.60 by
   applying 12 bps to one leg only. The engine was right at 4466.00. Docstring
   corrected and the numbers are now **pinned by a test**, so the docs cannot
   drift from the code again.
3. **Ingestion cursor double-count risk** (described above) — caught by writing
   the idempotency test before trusting the implementation.

### Verification

```
$ pytest Blue_engine\tests -q
184 passed in 50.02s
```

Live job run (platform down, cache-served inputs):

```
[tracker] BUY 300000 NIFTY@1.00   charges=0.00 pos=300000 cash=700,000
[tracker] BUY 150000 BANKNIFTY@1.00 charges=0.00 pos=150000 cash=550,000
equity: nav=1,000,000 gross=450,000 date=2026-09-25
```

`data/portfolio_nav/2026-09.jsonl` → NAV 1,000,000 · gross 450,000 · 2 positions
· regime/optimiser/vol telemetry carried for later slicing.

---

## Phase 2 — Risk & performance metrics — ⏳ NEXT

> Consumes `get_equity_curve()`. No new data sources needed.

| # | Deliverable | Notes |
|---|---|---|
| 1 | Annualised return/vol, Sharpe, Sortino, Calmar | daily returns from Phase 1 |
| 2 | Max drawdown + duration, hit rate, profit factor | underwater series for the chart |
| 3 | VaR / CVaR at 95/99% | historical + Cornish-Fisher (fat tails are the NSE norm) |
| 4 | Stress tests | ±1% NIFTY gap, sector crash, 3× spread widening |
| 5 | Rolling Sharpe / rolling vol series | feeds the dashboard Efficiency tab |

**Guard:** reuse `attribution/perf.py` where the maths already exists; do not
re-implement Sharpe. New code should be VaR/CVaR/stress only.

---

## Phase 3 — Exposure & attribution — ⏳

| # | Deliverable | Notes |
|---|---|---|
| 1 | Sector / market-cap exposure | map NSE symbols → Nifty sector buckets |
| 2 | Beta-weighted exposure vs NIFTY | needs Phase-2 covariance (or `risk/beta.py`) |
| 3 | P&L attribution waterfall: symbol → strategy → time-of-day | India microstructure: open/mid/close buckets |
| 4 | Execution quality: fill vs arrival/VWAP slippage | joins to `core/ledger.py` |

---

## Phase 4 — Optimiser layer — ⏳

| # | Deliverable | Notes |
|---|---|---|
| 1 | **HRP** as the robust default | no covariance inversion → survives correlated Indian equity |
| 2 | Black-Litterman with views from the signal layer | views already flow via `portfolio/views/` |
| 3 | Constraints: per-symbol, sector, gross/net, kill-switch | kill-switch overrides everything |
| 4 | Rebalance triggers: drift threshold + EOD, never intraday churn | cost-aware band already exists |

> **Note:** the optimiser *primitives* (ERC, HRP, NCO, BL, min-var, max-Sharpe,
> CVaR) already exist in `portfolio/optim/`. Phase 4 is about **choosing and
> constraining** them from the manager layer, not writing new maths.

---

## Phase 5 — ML / AI overlay — ⏳

| # | Deliverable | Notes |
|---|---|---|
| 1 | Regime model (HMM/GMM on vol, ATR%, breadth) → leverage multipliers | crisis regime → de-risk |
| 2 | CNN on candlestick heatmaps → short-horizon signal | feeds BL views |
| 3 | LSTM/Transformer next-day return forecaster | feature store: OHLCV, F&O OI, IV skew, breadth |
| 4 | RL (PPO) position sizing | state: regime, drawdown, exposure, VIX |
| 5 | NLP/GPT nightly sentiment → per-sector tilt | already scaffolded in `portfolio/views/nlp_views.py` + `llm_note.py` |

**Guard (non-negotiable):** ML output is **advisory** to the rule-based risk
layer. The risk layer always holds veto — circuit breakers and the drawdown
kill-switch are not ML-gated.

---

## Phase 6 — Dashboard — ⏳

| # | Panel | Data source |
|---|---|---|
| 1 | Equity curve + underwater (drawdown) plot | `get_equity_curve()` |
| 2 | Rolling Sharpe | Phase 2 |
| 3 | Exposure donut (sector) + long/short bar | Phase 3 |
| 4 | Risk table: VaR, CVaR, leverage, days-to-recover | Phase 2 |
| 5 | Optimiser what-if + trade-list preview | Phase 4 |

Existing `dashboard/pages/portfolio.py` stays as the live OMS/EMS view.

---

## Cross-cutting rules (apply to every phase)

1. **Pure core, best-effort I/O.** Engines compute; jobs persist. Never both.
2. **Degrade, never halt.** No key / no network / empty data → documented
   fallback + warning, never an exception.
3. **One definition of return.** Everything reads Phase 1's curve.
4. **Credentials via env → `_restored/.env` only.** Never logged, never committed.
5. **Every phase lands with tests.** A phase is not done until `pytest` is green
   and the docstring examples are pinned by assertions.