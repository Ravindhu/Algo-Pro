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

## Phase 2 — Risk & performance metrics — ✅ DONE

> Consumes `get_equity_curve()`. No new data sources needed.

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Sharpe, Sortino, Calmar, IR, capture (reused, not re-implemented) | [x] | `attribution/perf.py` |
| 2 | **Underwater curve** (drawdown path paired with dates) | [x] | `risk_report.underwater()` |
| 3 | **Drawdown duration + recovery state** | [x] | `risk_report.drawdown_stats()` |
| 4 | **Rolling vol & Sharpe** (aligned 1:1 with returns) | [x] | `risk_report.rolling_series()` |
| 5 | VaR / CVaR 95 & 99 (historical, Gaussian, Cornish-Fisher) | [x] | `risk/tail.py` (fixed) |
| 6 | Stress scenarios scaled by book beta, in currency | [x] | `risk_report.risk_report()` |
| 7 | One-call report from a NAV path | [x] | `risk_report.report()` |
| 8 | Manager API | [x] | `portfolio_manager.get_risk_report()` |
| 9 | Tests (28) incl. 4 regression tests for the bugs below | [x] | `tests/test_risk_report.py` |

### Bugs found and fixed in Phase 2

**1. `parametric_var` and `cornish_fisher_var` silently returned 0.0000 for every
input (PRODUCTION BUG, pre-existing).**

``_z_score(level)`` returns the *left-tail* quantile, which is **negative**
(−1.6449 at 95%). The VaR formula was written ``-(mu - z*sd)``, which with a
negative z computes ``-(mu + 1.645σ)`` — a **gain** — and then ``max(..., 0)``
clipped it to exactly zero. So the risk report said *“no risk”* regardless of the
data. On Indian small/mid-caps, where the tail is the whole point, this is the
worst possible failure mode: a number that looks like a measurement and is
guaranteed wrong.

Correct form: ``-(mu + z*sd)``. Verified against theory — on 4,000 Gaussian
returns all three estimators now land within 15% of ``1.645σ``.
**Test:** `test_regression_parametric_var_is_not_silently_zero`.

**2. Drawdown recovery was measured against the wrong peak (MY BUG).**

The recovery scan rebuilt the peak by replaying ``rs[:peak_index]`` from 1.0.
Whenever the high-water mark was **not at index 0** that rebuild was wrong —
e.g. ``[+50%, −10%, −5%, +2%]`` has a true peak of 1.50, but the replay gives
1.00, so the curve looked “recovered” the moment it climbed back above 1.00
while still 12.8% under water. A PM would be told a hole was filled when it was
not.

Fixed by tracking the actual high-water **value** at each step and comparing
recovery against it, scanning only *after* the trough.
**Test:** `test_regression_recovery_loop_does_not_self_satisfy` — asserts
``recovered`` and ``current_drawdown`` can never contradict each other.

**3. `months` in `read_nav`/`get_equity_curve` counts monthly FILES (USABILITY BUG).**

NAV is one file per month, so ``months=1`` means “the current month’s file”, not
“the last 30 days”. A caller reading 80 days of history with ``months=1``
silently got a **truncated** curve (18 rows instead of 79) — and every metric
computed from it would be wrong. Added `_window_warning()`: when the window
starts on the 1st of a month, the report now says so explicitly rather than
returning a confident number from the wrong sample.

### Verification

```
$ pytest Blue_engine\tests -q
212 passed in 51.56s
```

On a 90-day synthetic path (σ=1%/day, β=1.15):

```
VaR 95%: historical 0.0163 · Gaussian 0.0163 · Cornish-Fisher 0.0166
CVaR 95%: 0.0209        (correctly the most severe)
Sharpe / Sortino / Calmar / maxDD: populated
Stress worst case: COVID_2020
```

---

## Phase 3 — Exposure & attribution — ✅ DONE

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Sector / bucket exposure (aggregates same-bucket sleeves) | [x] | `exposure.py` |
| 2 | Long/short split, net-to-gross | [x] | `exposure.long_short_split()` |
| 3 | Beta exposure: measured > desk-assumed > 1.0, blend flagged | [x] | `exposure.beta_exposure()` |
| 4 | Concentration: HHI, effective bets, largest sleeve/sector | [x] | `exposure.concentration()` |
| 5 | Look-through (an index sleeve is not one bet) | [x] | `exposure.lookthrough()` |
| 6 | P&L by sleeve, **gross vs net** (cost problem vs signal problem) | [x] | `pnl_attribution.by_sleeve()` |
| 7 | P&L by **IST session bucket** (PRE/OPEN/MID/CLOSE/AFTER/WEEKEND) | [x] | `pnl_attribution.by_bucket()` |
| 8 | P&L by strategy label | [x] | `pnl_attribution.by_strategy()` |
| 9 | **Reconciliation guard** — every view must sum to the total | [x] | `pnl_attribution.reconcile()` |
| 10 | Manager API | [x] | `get_exposure()`, `get_attribution()` |
| 11 | Tests (48) | [x] | `test_exposure.py`, `test_pnl_attribution.py` |

### Bug found and fixed in Phase 3

**Sleeve→sector matching filed IT sleeves under Large Cap (MY BUG).**

``sector_of`` originally took the **first** substring match. ``"NIFTY IT"``
contains both ``"NIFTY"`` and ``"IT"``, and ``"NIFTY"`` appears first in the
map — so an IT sleeve was reported as Large Cap. Switching to **longest**-match
was not enough either: in ``"NIFTYIT"`` the generic token ``NIFTY`` (5 chars) is
longer than the specific ``IT`` (2), so it still lost.

The correct rule is **rightmost match, longest at that position**. NSE sleeve
names qualify a family name at the *end* (``NIFTY IT``, ``NIFTY PHARMA``,
``NIFTY AUTO``), so the qualifier is the informative token and sits furthest
right. Fixed and covered by `test_sector_of_exact_and_substring`.

This mattered because sector exposure is the *one* thing the module exists to
get right: overstating Large Cap while understating IT would have made a
concentrated tech book look diversified.

### Design decisions worth remembering

* **An index sleeve is not one bet.** ``NIFTY`` is ~35% Financials by
  construction, so a 60% NIFTY sleeve is already ~21% Financials *before* any
  BANKNIFTY sleeve is added. `lookthrough()` unwinds this; without it a desk adds
  a bank sleeve believing it is diversifying.
* **Gross and net P&L are never shown alone.** A sleeve with positive gross and
  negative net has a **cost** problem (widen the no-trade band, slow the cadence),
  not a signal problem — completely different fixes.
* **A fill after 15:30 IST is a red flag, not a bucket.** `AFTER` is surfaced
  loudly because cash equity cannot trade then; it almost always means a
  timestamp or timezone bug, and silently folding it into CLOSE would hide that.
* **Attribution must reconcile.** All three views are checked against their own
  totals; a non-reconciling table sets ``ok=False`` rather than being presented
  as fact, because someone will trade on the gap.

### Verification

```
$ pytest Blue_engine\tests -q
260 passed in 53.70s
```

---

## Phase 4 — Optimiser policy layer — ✅ DONE

> The optimiser *primitives* (ERC, HRP, NCO, BL, min-var, max-Sharpe, CVaR)
> already existed in `optim/`. Phase 4 adds the **policy layer** that chooses,
> constrains, and — most importantly — decides whether to trade at all.

| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Decision pipeline: kill-switch → data → regime → optimiser → constraints | [x] | `optim_policy.decide()` |
| 2 | **Kill-switch**: drawdown + daily-loss, latching, explicit reset | [x] | `optim_policy.KillSwitch` |
| 3 | **Data validity gate**: staleness, short sample, degenerate covariance | [x] | `optim_policy.check_data_validity()` |
| 4 | Regime-selected optimiser (single source: `optim.registry`) | [x] | `optim_policy.decide()` |
| 5 | Constraint resolution: defaults < desk limits < regime overrides | [x] | `optim_policy.resolve_constraints()` |
| 6 | Halt flows as a flat book through the normal weight path | [x] | `optim_policy.apply_to_problem()` |
| 7 | Manager API (drawdown auto-read from the equity curve) | [x] | `get_policy()` |
| 8 | Tests (27) | [x] | `test_optim_policy.py` |

### Design decisions that matter

* **The kill-switch is FIRST and overriding.** A desk's losses rarely come from
  picking the wrong objective function; they come from *continuing to trade*
  through a drawdown, a data fault, or a broken book. Making "stop" a
  first-class, testable decision is the single highest-value thing in Phase 4.
* **It is also LATCHING.** A switch that clears as soon as the drawdown ticks
  back inside the limit is not a kill-switch — it is a stop-loss, and it
  re-enters at the exact moment the desk is least able to judge whether the
  cause is fixed. Clearing requires an explicit named confirmation token.
* **Stale data is a HALT, not a warning.** This is subtle and important: sleeves
  whose prices stopped updating show **zero** correlation, so the optimiser
  treats them as independent and *concentrates into them*. A data fault does not
  produce an obviously wrong book — it produces a confidently concentrated one.
* **A halt is a flat book, not an exception.** POV, the risk gate and the
  dashboard all understand weights, so returning a legitimate zero-weight book
  (with a recorded reason) makes the halt visible in all the usual places and
  lets the caller use one code path for both cases.
* **Every decision carries reasons.** A silent halt is unusable in practice.

### Bug found and fixed in Phase 4

**Circular import from the plugin auto-registry.** ``optim_policy.py`` was
written with ``from . import registry``, but ``core/registry.py``
auto-discovers *every* module under ``portfolio/`` at import time. That made the
new module participate in its own import cycle and fail on first use. Fixed by
importing the concrete path (``from .optim import registry``).
*Note for future modules: never import the package-local ``registry`` by
relative shorthand from inside ``portfolio/``.*

### Verification

```
$ pytest Blue_engine\tests -q
287 passed in 51.03s
```

Live check (platform down, no provider data):

```
action=HALT  trade=False  optimiser=None
reasons: ['HALT: universe has 0 sleeve(s); need >= 2 to allocate']
```

Halted with an explicit reason rather than “optimising” an empty book — the
correct behaviour when the broker feed is unavailable.

---

## Phase 5 — ML / AI overlay — ⏳ NEXT

| # | Deliverable | Status | Notes |
|---|---|---|---|
| 1 | Regime model (HMM/GMM on vol, ATR%, breadth) → leverage multipliers | [x] | already in `economics/models/hmm_regime.py` |
| 2 | GBM/XGBoost sleeve-return forecast → BL views | [x] | already in `portfolio/views/ml_views.py` |
| 3 | Lexical + GPT sentiment → sector tilt | [x] | already in `portfolio/views/nlp_views.py`, `llm_note.py` |
| 4 | CNN on candlestick heatmaps | [ ] | new — short-horizon signal |
| 5 | LSTM/Transformer next-day forecaster | [ ] | new — feature store: OHLCV, F&O OI, IV skew, breadth |
| 6 | RL (PPO) position sizing | [ ] | new — state: regime, drawdown, exposure, VIX |

**Guard (non-negotiable):** ML output is **advisory** to the rule-based risk
layer. The kill-switch, circuit breakers and data-validity gates are *not*
ML-gated — a model cannot talk the desk into trading through a drawdown.

> Items 1–3 already exist and are tested. Phase 5 in practice means **wiring
them into the policy decision** (views → BL → optimiser, behind `views_enabled`)
and then adding the CNN/LSTM/RL work as research.

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