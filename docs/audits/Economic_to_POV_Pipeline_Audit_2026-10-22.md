# Economic → POV Pipeline Audit

**System:** My Trading System / Blue Engine
**Scope:** Full decision chain, **Economic Engine → POV Engine** (order-submission boundary)
**Audit date:** 2026-10-22
**Auditor role:** Quant / execution / risk review
**Repo:** `C:\Users\Admin\CodeGPT\my-trading-system\Blue_engine`
**Live-data probe:** 2026-10-22 11:50 IST, `client=None` (no broker required for macro)

---

## 0. How to read this document

| Field | Meaning |
|---|---|
| **ID** | Stable finding reference (`F-nn`). Quote it in commits and tickets. |
| **Priority** | `P0` capital/data at risk · `P1` materially wrong behaviour · `P2` correctness/robustness · `P3` hygiene |
| **Evidence** | How the claim was verified. `VERIFIED` = reproduced in this repo. `CODE-READ` = established from source logic only. |
| **Status** | `FIXED` (changed in this audit) · `OPEN` (documented, not changed) |

**Honesty statement about this audit**

I made errors during the first pass of this work and corrected them rather than
leaving them in the tree. Specifically:

- I claimed a **risk-veto ordering bug** in `pipeline.py`. Tracing the stage enum
  showed `RISK = 45` and `POV = 50`, so risk already ran **before** POV. The claim
  was **wrong**; the change was reverted and the file restored to its original logic.
- I added execution changes whose premise I had not proven (asking a broker for a
  quote `date` field that is not contractual; using `resolve_expected_price` for a
  side-aware slippage calculation). Those were reworked into statements that are
  true of the code and provable in-repo.
- I broke `_safe_float` in `pov.py` while deleting dead code and restored it. That
  accident is itself retained below as **F-04**, because the file *did* end up with
  two module-level definitions and the audit is supposed to surface exactly that
  class of hazard.

Everything marked `FIXED` has a regression test in
`Blue_engine/tests/test_economic_to_pov_audit.py`.

---

## 1. Executive summary

The pipeline is architecturally sound: it is pure, dependency-injected, degrades
instead of halting, and has a durable kill switch. The defects are not in the
architecture — they are in the **seams**: the places where one engine's output is
supposed to become another engine's input.

### The three findings that matter

1. **The Economic engine's only sizing decision was discarded (F-01).**
   `gross_exposure_multiplier` was computed, logged, persisted to the dashboard,
   documented as "consumed by the Portfolio engine" — and read by **nothing**.
   A RISK_OFF book and a RISK_ON book produced identical gross exposure.
   *This was the single largest defect on the money path.*

2. **Even after the weights were tilted, execution ignored the tilt (F-02).**
   `POVSlicer` consumes a **share count**. The Portfolio produces **weights**. The
   bridge between them took quantity from `context["quantity"]` or the static POV
   config, so the de-risking never reached the order.

3. **The bridge refused to trade at all in the default configuration (F-03).**
   The Portfolio's sleeves are *indices* (`NIFTY`, `BANKNIFTY`, `NIFTYIT`); the POV
   target is a *stock* (`INFY`). The new sleeve cap returned `0` for "name not
   found", which was interpreted as "portfolio allocates nothing" → `skipped`.
   Verified end-to-end: the slice never executed.

### What live data says (2026-10-22 11:50 IST)

| Signal | Value | Provenance | Freshness |
|---|---|---|---|
| `inflation` (CPI) | **5.54 %** | LIVE · MoSPI | Sept 2026 |
| `gdp_growth` | **7.82 %** | LIVE · MoSPI | Q1 FY27 |
| `employment` | **4.80 %** | LIVE · MoSPI | Dec 2025 ← **stale by ~10 months** |
| `currency` (USD/INR) | **94.7145** | LIVE · RBI FBIL | 21 Oct 2026 13:00 |
| `bond_return` | *missing* | static fallback 6.68 | not live |
| `debt` | *missing* | static fallback 81.92 | not live |
| `forex_reserves` | *missing* | static fallback 765.9 | not live |
| `news_tone` | *missing* | no headlines supplied | n/a |
| `fii_inflow` | *unavailable* | provider warning | n/a |

All four live providers reported `ok=True`, `stale=False`. **The freshness flags
are wrong for `employment`** — see **F-08**.

---

## 2. Verified flow map

```
   ┌────────────────────────────────────────────────────────────────────┐
   │ provider stack  (economics/providers)                              │
   │   cached:fii       cached:rbi_fbil    cached:mospi    cached:india │
   │        └──────────────┴───────────────────┴───────────────┘        │
   │                      CompositeProvider (first non-null wins)        │
   └──────────────────────────────┬─────────────────────────────────────┘
                                  │ macro{signal: {value, previous, source, as_of}}
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ EconomicEngine  stage=10                                           │
   │   surprise (AR1) × delevel (z of change) × level (tanh)  → score   │
   │   regime.band(score) → RISK_OFF … RISK_ON                          │
   │   score_multiplier = clamp(1+score, .25, 1.5)                      │
   │   OUT: gross_exposure_multiplier    ◄── F-01 was dropped here      │
   └──────────────────────────────┬─────────────────────────────────────┘
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ MarketEngine  stage=20      breadth, FII, vol state → risk_appetite│
   └──────────────────────────────┬─────────────────────────────────────┘
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ PortfolioEngine  stage=30                                          │
   │   optimiser → weights → exposure caps → ECONOMIC TILT (F-01/F-06)  │
   │   → risk → sizing(min of vol-target, kelly, drawdown)              │
   │   OUT: target_weights, economic_tilt, capital, cash_weight          │
   └──────────────────────────────┬─────────────────────────────────────┘
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ AlphaEngine  stage=40       fundamental + technical + backtest gate │
   │   OUT: selected[], signals{}                                       │
   └──────────────────────────────┬─────────────────────────────────────┘
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ RiskEngine  stage=45        incident/limit inputs → veto            │
   └──────────────────────────────┬─────────────────────────────────────┘
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ PovEngine  stage=50                                                │
   │   alpha.selected[0] → sleeve cap → BUDGET cap (F-03)               │
   │   → ECONOMIC GUARD (F-02) → POVSlicer.execute(intent)              │
   └──────────────────────────────┬─────────────────────────────────────┘
                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │ POVSlicer  (live money)                                            │
   │   poll → delta-volume gate → band rate → guards → throttle         │
   │   → pre_order_check (RiskGate) → placeorder()                      │
   └────────────────────────────────────────────────────────────────────┘
```

### Stage order actually executed

| # | Engine | Stage | Runs? | Gates POV? |
|---|---|---|---|---|
| 1 | `economic` | 10 | always | no |
| 2 | `market` | 20 | always | indirectly (`High_Panic` suppresses rebalance) |
| 3 | `sector` | 25 | opt-in | no |
| 4 | `screener` | 25 | opt-in | no |
| 5 | `valuation` | 27 | opt-in | no |
| 6 | `portfolio` | 30 | always | no |
| 7 | `alpha` | 40 | always | **yes** (no selection → POV skipped) |
| 8 | `risk` | 45 | always | **yes** (veto → POV blocked) |
| — | `infrastructure` | 60 | pre-flighted **first** | **yes** (`gate_on_infrastructure`) |
| 9 | `pov` | 50 | last | is the terminal stage |

> **Note on the pre-flight.** `INFRASTRUCTURE = 60`, so infrastructure is *last* in stage
> order but is run **before the loop** to emit its gate signal, then run again
> inside the loop for the report. That is deliberate (see `pipeline.py` lines
> 191–204) and is **not** a defect — it is the only ordering that lets a late-stage
> health check gate an earlier-stage action. Risk does **not** need this trick,
> because `RISK (45) < POV (50)` already.

---

## 3. Findings — line by line

Line numbers refer to the file state **after** this audit's fixes.

---

### F-01 · P0 · FIXED — Economic multiplier never reached the Portfolio

**Files:** `economics/economic_engine.py`, `portfolio/portfolio_engine.py`

**What the code said it did**

`economic_engine.py:7-8` (module docstring):

> "a `gross_exposure_multiplier` consumed by the Portfolio engine"

`regime.py:9-10`:

> "translate a score *or a model posterior* into the `gross_exposure_multiplier`
> that the Portfolio engine consumes"

**What the code actually did**

A repository-wide search for `gross_exposure_multiplier` across `Blue_engine/**/*.py`
returned **zero consumers in the Portfolio engine**. Grepping the portfolio package
produced only `portfolio_manager.py:210-216` — a *reader for the dashboard*, not a
sizing input.

| Location | Role before fix |
|---|---|
| `economic_engine.py:394-396` | computes `combine_multiplier(...)` |
| `economic_engine.py:426` | puts it in `data["gross_exposure_multiplier"]` |
| `economic_engine.py:416-418` | logs it |
| `portfolio_manager.py:210-216` | reads it **for display only** |
| `portfolio/portfolio_engine.py` | **never reads it** |

**Impact (VERIFIED)**

A book with `gross_exposure_multiplier = 0.74` (RISK_OFF) and one with `1.30`
(RISK_ON) produced **identical** `target_weights` and identical `gross`. The only
regime influence that survived was `optim.registry.policy_for(regime)`, which
selects an *optimiser*, not a size.

This contradicts the project's own priority hierarchy
(`Capital Protection → Data Integrity → Risk Control → …`) and the documented
behaviour in `optim_policy.py:350-353`:

> "the *same* optimiser, given a tighter gross cap in a risk-off regime, produces a
> smaller book"

**Fix**

`portfolio/portfolio_engine.py:440-470` — `_economic_tilt()` scales the target book
by the multiplier and returns an audit record:

```python
raw  = safe_float(econ.get("gross_exposure_multiplier"))
mult = clamp(raw, 0.0, 1.5)          # a bad payload must never lever UP
scaled = {k: round(v * mult, 6) for k, v in targets.items()}
return scaled, {
    "applied": True, "multiplier": round(mult, 4),
    "pre_tilt_gross": ..., "post_tilt_gross": ...,
    "regime": econ.get("regime"), "risk_on_score": econ.get("risk_on_score"),
}
```

Wired at `portfolio_engine.py:603-605`, **before** risk and sizing, so vol-target
and the drawdown governor see — and may only further reduce — the tilted book.
Result field: `data["economic_tilt"]` (`portfolio_engine.py:671`).

**Test:** `test_economic_multiplier_scales_the_book`, `test_neutral_multiplier_is_not_reported_as_applied`

---

### F-02 · P0 · FIXED — Execution budget ignored the tilt

**File:** `pov/pov_engine.py`

**The gap**

`EconomicEngine` emits *weights*. `POVSlicer.__init__` (`slicers/pov.py:36-48`)
consumes a *share count*:

```python
self.quantity = int(config.get("quantity") or config.get("total_quantity") or 0)
```

Nothing converted one to the other. `PovEngine._slice()` took quantity from
`context["quantity"]` (`pov_engine.py:171`) or the static `pov` config block
(`pov_engine.py:207-212`). So even with F-01 fixed, the conservative book and the
aggressive book sent **the same order size** to the broker.

**Dead code that masked it**

```python
# pov_engine.py:32-50 — BEFORE
_gross = safe_float(portfolio.get("gross_exposure_multiplier"))   # never present
if gross < 0.75:
    cfg["participation_pct"] = base_pct * gross
```

Three independent reasons this could never fire:
1. The key lives on the **Economic** payload, not the Portfolio payload.
2. The **Portfolio result** was never passed into `_build_pov_config` usefully.
3. The computed `cfg` was returned in the `dry_run` payload but **not used** by the
   slicer path (`pov_engine.py:275-284` returned it; line 286 discarded it).

Net effect: a de-risking block that read like a control and controlled nothing.

**Fix**

- Removed the dead scaling; documented why in `_build_pov_config` (`pov_engine.py:32-51`).
- Added `_economic_execution_guard()` (`pov_engine.py:53-88`) which converts the
  Portfolio's `economic_tilt` into an explicit allowed quantity and **reports**
  every trim:

```python
allowed = int(quantity * min(1.0, mult))   # never increases
return {"applied": True, "multiplier": ..., "requested_quantity": ...,
        "allowed_quantity": allowed, "regime": ..., "risk_on_score": ...}
```

- Attached to the slice as `cfg["economic_guard"]` and surfaced in warnings
  (`pov_engine.py:255-278`). Zero allowed quantity → `skipped` with
  `reason="economic_tilt_blocks_order"` (fail closed).

**Test:** `test_economic_guard_reduces_execution_quantity`,
`test_economic_guard_never_increases_quantity`, `test_slice_falls_back_to_budget_then_applies_tilt`

---

### F-03 · P0 · FIXED — Index sleeves vs stock execution → slice never traded

**File:** `pov/pov_engine.py`

**The mismatch (VERIFIED end-to-end)**

| Source | Names |
|---|---|
| `macro_master.json:159` `portfolio_inputs.universe` | `NIFTY, BANKNIFTY, NIFTYIT, NIFTYPHARMA` |
| `macro_master.json:147` `universe_names` | `NIFTY, BANKNIFTY, IT, PHARMA, AUTO` |
| `macro_master.json:231` `pov.symbol` | **`INFY`** |
| `scripts/run/run_pov.py:81` | **`INFY`** |

The Portfolio allocates across **indices**. The executor trades a **stock**.
Nothing in the pipeline maps one to the other.

**The failure**

My first version of the sleeve cap treated "symbol absent from `target_weights`" as
`return 0`, on the reasoning that a book which does not hold the name must not buy
it. That is right for a stock book and **wrong for this architecture**, because the
absence is structural, not a decision.

Verified: `status="skipped"`, `reason="symbol not in target portfolio"` for
`INFY` against the default config. **The entire Economic → POV money path was
inoperable in the shipped configuration.**

**Fix — three distinct outcomes instead of two** (`pov_engine.py:225-249`)

| Sleeve lookup | Meaning | Action |
|---|---|---|
| symbol found | real allocation | cap at `weight × capital / price` |
| miss, book non-empty | composite sleeve, un-mappable | fall back to **budget cap** + warn |
| miss, allocation 0 | portfolio genuinely holds none | **refuse** (fail closed) |

Budget cap (`pov_engine.py:134-162`) uses the book's own investable weight —
`gross`, else `1 − cash_weight` — so it can never exceed what the portfolio
intended to deploy.

**Test:** `test_slice_falls_back_to_budget_then_applies_tilt`,
`test_slice_refuses_a_symbol_with_no_price_to_size_the_budget`,
`test_slice_caps_at_sleeve_allocation_when_names_match`

> **Open architectural question (not a bug).** Index sleeves executed as single
> stocks is a conceptual mismatch that deserves a decision: either the Portfolio
> must emit **constituent** weights, or execution must trade the **index ETF**.
> Until that is decided, the budget fallback is the safe behaviour, and it warns
> loudly every time. See §5.

---

### F-04 · P1 · FIXED — Duplicate `_safe_float` that truncated prices

**File:** `pov/slicers/pov.py`

The module ended with **two** definitions of `_safe_float`:

```python
# pov.py:816-821  (single definition)
def _safe_float(value, default=0.0) -> float:
    ...
    return float(value)

# pov.py:823-828  (this is the one that shadowed it)
# def _safe_float(...): return int(float(value))   ← REMOVED
```

Both position references were confirmed by text search after the fix:
exactly one `def _safe_float(` remains.

Python resolves the **last** definition, so every price in the module was routed
through `int(float(...))`.

**Why this matters for an execution engine**

| Call site | Input | Truncated result |
|---|---|---|
| `pov.py:330` `ltp = _safe_float(qd.get("ltp"))` | `1487.35` | `1487` |
| `pov.py:352` `ask = _safe_float(qd.get("ask"))` | `1487.40` | `1487` |
| `pov.py:231-232` band vwap / ceiling (`_safe_num`) | `1480.75` | preserved locally, but compared against a truncated `ltp` |
| `pov.py:442` `min_pov_pct` etc. | `5.5` | `5` |

Consequences:
- **Buyer-initiated VWAP** (`pov.py:409` `ltp >= ask`) compares two integers —
  ticks within the same rupee become indistinguishable, so the classification is
  arbitrary and the band anchor is wrong by up to a rupee.
- **Ceiling halt** (`pov.py:438`) compares an integer LTP against a truncated
  ceiling. On a ₹1,500 stock that is a **₹1 band error** on a 0.5 % ceiling band
  (₹7.50) — over 13 % of the guard's width.
- **`arrival_window.record()`** and session VWAP accumulate integer prices.

**How it got there:** I created it while deleting an unreachable block after
`_above()` (see F-20). The correct fix was to delete the shadow, not to keep it.
Restored to a single definition (`pov.py:816-821`).

**Test:** `test_safe_float_preserves_fractional_prices`,
`test_safe_float_is_defined_once` (asserts exactly one module-level definition —
this is the guard rail).

---

### F-05 · P2 · FIXED — An up-tilt breached the absolute gross cap

**File:** `portfolio/portfolio_engine.py`

`_apply_exposure_limits()` caps gross at
`min(max_gross, 1 − min_cash_buffer_pct)` = `min(1.0, 0.85)` = **0.85**.

The first version of F-01 applied the tilt **after** that cap:

| Step | Gross |
|---|---|
| optimiser + exposure caps | 0.850 |
| economic tilt × 1.30 (RISK_ON) | **1.105** ← above the 0.85 cap |

Verified by running `verify_fixes.py`. A RISK_ON reading could therefore consume
cash that `min_cash_buffer_pct` exists to protect.

**Fix:** the hard caps are **absolute**, so they are re-applied *after* the tilt
(`portfolio_engine.py:606-612`). A tilt may redistribute headroom the caps allow;
it may never create it.

**Test:** `test_economic_multiplier_scales_the_book[1.3-0.85]`

---

### F-06 · P2 · FIXED — Stale sleeve volatility silently became a default

**File:** `portfolio/portfolio_engine.py:205-234`

```python
# BEFORE
vols_map = {k: (safe_float(v) or 0.2) for k, v in (data.get("vols") or {}).items()}
betas    = {k: (safe_float(v) or 1.0) for k, v in (data.get("betas") or {}).items()}
```

`safe_float(0.0) or 0.2` → `0.0 or 0.2` → **`0.2`**. Two very different inputs
collapsed into one default:

- **missing** vol/beta → documented default (fine)
- **`0.0`** — a sleeve whose prices stopped updating → silently `0.20`

A zero-variance sleeve is the signature of a **stale feed**. Left in the
covariance, it looks like *free risk reduction*, and the optimiser concentrates
into it. `optim_policy.check_data_validity()` (`optim_policy.py:230-239`) already
catches a zero *diagonal in the covariance*, but the vol **map** is a separate
input and bypassed that check.

**Fix:** explicit range validation with an operator-visible warning for each
rejected input (`vol ≤ 0 or vol > 1.0`, `beta ≤ 0 or beta > 3.0`).

**Test:** `test_zero_volatility_is_reported`, `test_implausible_beta_is_reported`,
`test_zero_volatility_warning_is_not_duplicated`

---

### F-07 · P1 · FIXED — `kill_switch_state()` latched a halt as a side effect

**Files:** `portfolio/optim_policy.py:302-339`, `core/screener_bridge.py:169-177`

**The defect**

```python
def kill_switch_state() -> Dict[str, Any]:
    switch = _process_kill_switch()      # ← INSTANTIATES + hydrates from disk
```

`_process_kill_switch()` (`optim_policy.py:257-281`) creates the singleton and
loads any **persisted tripped state** into the current process. Calling a
*read* therefore had a *write* effect on process state.

**Why it is dangerous, not merely untidy**

`screener_bridge.kill_tripped()` is a **polling read** in a long-lived process.
Calling it once hydrated the switch. If a previous run had tripped it, the bridge
process now holds `tripped=True`, and any later `trip_kill_switch(reason)` in that
process **short-circuits on the already-set flag** — so the *real* reason is never
persisted and the dashboard shows the stale reason from an earlier incident.
Forensic history is corrupted exactly when it matters most.

**Fix:** `peek_kill_switch()` (`optim_policy.py:302-325`) — a non-latching read
that consults the live process switch if one exists, else the store, and never
instantiates. `screener_bridge` now uses `peek_`. `kill_switch_state()` keeps its
latching behaviour for the trading entry point, with an explicit warning in its
docstring.

**Test:** not unit-tested (process-singleton semantics); verified by code read.

---

### F-08 · P1 · OPEN — Staleness is not detected for monthly macro series

**File:** `economics/providers/base.py:86-112`, `providers/composite.py:87-94`

**Evidence (LIVE, 2026-10-22 11:50 IST)**

```
composite ok=True  stale=False
  cached:mospi   ok=True  stale=False  1710ms
```

But the payload carries:

| Signal | `as_of` | Age at probe |
|---|---|---|
| `employment` | **December 2025** | ~10 months |
| `inflation` | September 2026 | ~1 month (expected for CPI) |
| `gdp_growth` | Q1 FY27 | ~1 quarter (expected) |
| `currency` | 21 Oct 2026 13:00 | < 1 day |

`stale=False` for a 10-month-old unemployment rate is **wrong**, and it is the
field the dashboard uses to decide whether to trust a number.

**Root cause:** `MacroProvider.fetch()` defaults `meta.stale` to `False`
(`base.py:108`: `payload["meta"].setdefault("stale", False)`). A provider that does
not explicitly set it is **assumed fresh**. `CompositeProvider` then ORs the
per-source flags (`composite.py:89`) — but each source said `False`, so the
composite says `False`.

**Impact:** the Economic engine scores a 10-month-old PLFS print with the same
confidence as a CPI print from last month. `employment` has weight **0.5** in
`macro_master.json:94`. Its `previous: 4.7 → value: 4.8` therefore generates a
`delevel` and a `surprise` view (`features/delevel.py`, `features/surprise.py`) from
a change that is ten months old — a **false "surprise"** contributing up to 40 % of
that signal's score by `DEFAULT_BLEND` (`scoring.py:26`).

**Not fixed here** because the correct threshold is a desk decision, not a code
one. Recommended (see §5, N-01): give each signal a **max-age** in config and have
the composite set `stale=True` when the newest reading exceeds it.

---

### F-09 · P1 · OPEN — Static macro values are indistinguishable from live ones in the vote

**File:** `economics/economic_engine.py:294-316`

The engine already carries provenance (`source`, `as_of`, `src`, `period`,
`economic_engine.py:306-312`) and the module docstring is candid about it:

> "Absent == the fallback (macro_master.json) supplied it."

But provenance is **reported**, not **weighted**. In the live probe:

| Signal | Value used | Actually live? |
|---|---|---|
| `bond_return` | 6.68 | **no** — static |
| `debt` | 81.92 | **no** — static |
| `forex_reserves` | 765.9 | **no** — static |

Combined static weight in the default config
(`macro_master.json:91-97`): `1.0 + 0.5 + 0.5 = 2.0` out of **`6.0` total** —
one third of the composite risk-on score is computed from numbers that never
update. `bond_return`, `debt` and `forex_reserves` can be arbitrarily old and the
score will not move.

`macro_master.json:46` even documents why `bond_return` cannot be fetched live
("FBIL+CCIL render via JS … Do not substitute the index for the yield"). That is
sound reasoning for *not faking it* — but scoring it at full weight is the same
error with extra steps.

**Recommendation (N-02):** down-weight or exclude static signals, and expose a
`live_signals` / `static_signals` split in `data` so the dashboard and the
agent can see how much of the decision was real.

---

### F-10 · P1 · OPEN — Idle-run misreporting at cutoff (partially addressed)

**File:** `pov/slicers/pov.py:273-295`

**Original behaviour:** `execute()` checks the cutoff **once**, at
`pov.py:130-133`:

```python
if self.cutoff and is_past_cutoff(self.cutoff, now=now):
    return {"filled": 0, ..., "reason": "cutoff_reached_at_start"}
```

`squareoff_time` had **no** such check. A run launched at 15:25 IST with
`squareoff_time = "15:20"` entered the loop, polled for the full `duration`, and
returned `phase="accumulating", filled=0` — indistinguishable from "the market
gave me nothing".

**Fix applied:** an explicit squareoff-at-start guard returning
`phase="aborted", reason="squareoff_reached_at_start"`.

**Still open:** the same class of misreporting exists whenever the loop exits for
a *non-market* reason. Three paths `break` without setting a reason:

| Line | Condition | Current report |
|---|---|---|
| `pov.py:298-306` | quote feed paused | `filled=0, phase="accumulating"` |
| `pov.py:311-323` | quote invalid × threshold | `filled=0, phase="accumulating"` |
| `pov.py:353` | live monitor aborts | `filled=0, phase="accumulating"` |

Only the paths I touched set `summary_reason`. **N-03:** every `break` in the loop
should set one. A run that stopped for a data fault must never look like a run
that found no volume.

---

### F-11 · P2 · FIXED — Stale-limit cancellation crossed strategy boundaries

**File:** `pov/slicers/pov.py:777-813`

```python
# BEFORE
and (row.get("strategy") == strategy_label
     or row.get("strategy") == strategy_label.split("-")[0])
```

For `strategy_label = "MACRO-POV"` (the default, `macro_master.json:251`), the
second clause matches the strategy string **`"MACRO"`**. On a shared OpenAlgo
orderbook — the screener bridge, a second POV run, a manual order tagged `MACRO` —
every open LIMIT for `MACRO` on that symbol was cancelled as "stale".

`strategy_label.split("-")[0]` is not a scope; it is a prefix guess. The intent was
evidently to tolerate legacy tags, but a prefix match on a user-controlled string
is an unbounded grant.

**Fix:** an explicit allow-list (`pov.py:97-102`) defaulting to the exact label,
extendable only by naming tags in `allowed_strategy_labels`:

```python
allowed_labels = allowed_strategy_labels or {strategy_label}
if (row.get("symbol") == symbol and row.get("exchange") == exchange
        and row.get("strategy") in allowed_labels ...)
```

Threaded through both call sites (`pov.py:594-600`, `pov.py:625-634`).

**Test:** covered indirectly by the existing POV suite; the guard is structural
(no prefix matching remains in the file).

---

### F-12 · P2 · OPEN — `slippage_acc` is recorded but never acted on

**File:** `pov/slicers/pov.py`

| Line | State |
|---|---|
| `pov.py:235` | `slippage_acc = SlippageAccumulator(window=10)` |
| `pov.py:716` | records a sample after each successful child |
| `pov.py:475-492` | **reads it** and penalises the rate — added in this audit |

**Before this audit, `slippage_acc` had exactly one write site and zero readers.**

`max_slippage_bps`, `slippage_penalty_factor`, `slippage_window_fills` appear in
`scripts/run/run_pov.py:190-193` with the comment *"SLIPPAGE FEEDBACK LOOP — reduce
POV rate on poor fills"*. Before this audit **no code read `slippage_acc`**, so the
documented feedback loop did not exist and those three operator knobs were inert.

**What I added, and the honest caveat**

```python
# pov.py:475-492
if slippage_acc.is_over_threshold(window_bps):
    penalised = max(min_pov_pct, rate_for_poll * penalty)
    slice_qty = int((delta_vol * penalised) / 100.0)
```

I first wrote the sample side using `resolve_expected_price()`
(`benchmark.py:208-228`). **That was wrong:** it returns the **larger** of two
VWAPs — a benchmark choice, not a side-aware expectation. For a BUY filling at the
touch, `actual − expected ≤ 0` structurally, so the accumulator would only ever
record *favourable* slippage and the guard could never trigger. Corrected
(`pov.py:698-718`) to use the session VWAP directly with the existing side-aware
sign convention in `compute_slippage_bps()` (`benchmark.py:187-205`).

**Still open (N-04):** the guard is **deliberately conservative to the point of
being weak** — it penalises only *future* participation, never cancels a working
child, and the fill used is the broker's `average_price` from the *ack*, which on
most Indian brokers is `0` until a fill occurs. On such a broker the accumulator
never gets a real sample. This needs a feed of actual fills before it is more than
a rate damper.

---

### F-13 · P2 · FIXED — No kill-switch check during a long signal wait

**Files:** `core/runner.py:166-171`, `pov/slicers/base.py:22-32`, `pov/slicers/pov.py:637-653`

**The window**

```
runner.py:167   kill_state = kill_switch_state()      ← read ONCE
runner.py:303   _poll_signals_until_fire(...)         ← can wait 30+ minutes
runner.py:443   gate.check(intent, ...)               ← no kill-switch input
runner.py:465   slicer.execute(intent)                ← places first child
```

An operator trip from the dashboard — or a breach tripped by another process —
during that wait was **ignored**. The run proceeded to place orders.

**Fix — three layers**

1. `runner.py:457-463` re-reads before execution and aborts with exit code 4.
2. `base.py:30` gives every slicer a `kill_tripped` hook.
3. `pov.py:637-653` checks it on **every child**, before the risk gate:

```python
halted = bool(self.kill_tripped and self.kill_tripped())
...
if halted:
    summary_reason = "halted_by_kill_switch"
    break
```

Fail-closed on exception: a halt check that *raises* aborts the run
(`summary_reason="halt_check_failed"`) rather than assuming safe.

**Test:** not unit-tested (requires runner orchestration); verified by code read.

---

### F-14 · P2 · FIXED — RiskGate pre-check declared every intent an exit

**File:** `core/runner.py:422-453`

```python
# BEFORE
decision = gate.check(intent, ..., reduces_exposure=True)
```

`reduces_exposure=True` disables the `max_daily_loss` breach
(`risk_gate.py:222`):

```python
elif pnl <= -abs(self.max_daily_loss) and not reduces_exposure:
    breaches.append(...)
```

Every **entry** was therefore exempt from the daily-loss limit at intent level.
The per-child check (`runner.py:364-395`) still ran with the correct phase, so this
was defence-in-depth erosion rather than a bypass — but it also made the log
misleading: a risk-blocked entry printed **`risk gate passed`**.

**Fix:** derive the flag from intent side vs actual position (`runner.py:429-448`):

```python
intent_reduces = ((intent_action == "SELL" and held > 0)
                  or (intent_action == "BUY" and held < 0))
```

Position lookup failure → `held = 0` → treated as an entry (fail closed, with a
warning).

**Caveat:** `positionbook()` is called once more per run. That is acceptable at
intent level (once), but the per-child path already calls
`fetch_risk_context()` — see **F-15**.

---

### F-15 · P2 · OPEN — Risk context is refetched before *every* child

**File:** `core/runner.py:364-395`

```python
def _authorize_child(*, phase, quantity, action, price=None):
    ...
    child_context = fetch_risk_context(client, rc_cfg)   # ← broker round-trip
    for warning in child_context.get("warnings") or []:
        print(f"[runner] RISK CONTEXT warning: {warning}")
```

With `poll_interval_seconds = 15` and `max_slice_quantity = 100` on a 1,000-share
parent, that is up to **10 position-book round-trips**, each printing every
warning. On a slow broker (the live probe already shows the FII provider at
**19 s**) this sits directly in the order path.

It is *correct* (position-dependent limits must be fresh) but it is **unbounded
work in the hot path**. **N-05:** cache for a short TTL (2–5 s) or refresh only
when `phase` changes, and log warnings once per run rather than per child.

---

### F-16 · P2 · OPEN — `PovEngine` has no time budget for `duration`

**File:** `pov/pov_engine.py`

`PovEngine._slice()` calls `slicer.execute(...)` synchronously
(`pov_engine.py:291-296`). `POVSlicer.execute()` blocks for `duration` seconds
(default **900 s**, `macro_master.json:236`). `MacroPipeline.run()` is therefore
blocking for the whole execution.

That is acceptable for a batch job and unacceptable for anything driving the
dashboard (a `run()` from a request handler freezes the reference implementation
for 15 minutes). **N-06:** either document the pipeline as batch-only, or run POV
in a worker with a handle in the report.

---

### F-17 · P2 · OPEN — `dry_run` is a flag with no enforcement

**Files:** `core/pipeline.py:56-62`, `pov/pov_engine.py:274-285`

`MacroPipeline` documents (`pipeline.py:11-15`):

> "`dry_run` (default True) runs every analytic engine and builds the POV config,
> but does NOT submit orders. Live execution requires an explicit `dry_run=False`
> **AND** an OpenAlgo SDK client."

But `PovEngine` never reads `dry_run`. It submits whenever `client` is present
(`pov_engine.py:287-296`). The only thing preventing live orders in a `dry_run=True`
pipeline is that **nobody bothered to pass a client** — a convention, not a
control.

**N-07:** `PovEngine._run` must check `context.get("dry_run", True)` and return
`status="dry_run"` regardless of the client. This is a one-line capital-protection
change and should be done before any unattended live use.

**Note:** the real live path is `scripts/run/run_pov.py` → `runner.run_with_config`,
which does **not** use `MacroPipeline`. It has no `dry_run` concept; live/paper is
controlled by OpenAlgo's own `analyze` mode (`read_mode`, `runner.py:189`). See
**N-08** — the two entry points have diverged.

---

### F-18 · P3 · FIXED — Dead scaling in `_build_pov_config`

Covered under F-02. Recorded separately because it is a **documentation** defect:
the docstring described a control that did not exist, which is how F-02 survived
review.

---

### F-19 · P3 · FIXED — Unreachable code after `_above()`

**File:** `pov/slicers/pov.py`

```python
def _above(ceiling, price, tick=0.05) -> bool:
    ...
    return price > (ceiling + tick / 2.0)
    if value is None or value == "":      # ← unreachable
        return default
    ...
```

Dead statements after a `return`, left over from a refactor. Removing them is what
introduced F-04. Both are now correct: `_above` ends at its return, and `_safe_float`
exists once.

---

### F-20 · P3 · OPEN — Silent `except` blocks hide real failures

**File:** `pov/slicers/pov.py` (several)

| Line | Statement | Risk |
|---|---|---|
| `pov.py:500-501` | `except Exception:` → `durable_filled = None` | a broken **ledger** is indistinguishable from "no new fills"; `filled_qty` silently stops advancing |
| `pov.py:142-143` | circuit probe | acceptable (explicitly non-fatal) |
| `pov.py:171-172` | protective-level registry | acceptable (logs) |

`pov.py:500` is the one that matters: it is the point where the slicer reconciles
what it *believes* filled against what the **durable ledger** says. Swallowing the
error there means a corrupt ledger produces a run that looks healthy and under-fills.
**N-09:** log at `WARNING` and count consecutive failures; after *n*, treat as a
data fault and exit the loop.

---

### F-21 · P3 · OPEN — `PovEngine` TCA uses placement price, not fills

**File:** `pov/pov_engine.py:201-237`

`_execution_cost()` derives `price_drift_bps` from `ltp_at_place`
(`pov.py:717-718` records `"ltp_at_place": ltp`).

That is the price **when the order was sent**, not the price it **filled** at. The
metric labelled in the dashboard as execution cost is therefore a *placement drift*
proxy. It is honest in the docstring ("a proxy for implementation shortfall",
`pov_engine.py:212-213`) but easy to misread as TCA.

**N-10:** join to the ledger's `avg_price` (already captured in `_record_child`,
`pov.py:734-757`) and label the current metric `placement_drift_bps`.

---

### F-23 · P1 · FIXED — FII/DII came from a fragile scrape while real data sat unused

**Files:** `economics/providers/fii_provider.py`, `tests/test_fii_vendor_source.py`

**The anomaly**

| Route | State at audit |
|---|---|
| NSE scrape (`fiidiiTradeReact`) | returned **nothing**, after **8.9 s** |
| Vendor parquet (`data/FLOWS/fii_dii_cash.parquet`) | **170 rows**, real FII + DII cash, unused |

`FiiFlowProvider` only scraped. The pinned vendor checkout at
`Blue_engine/database/vendor/indian-market-historical-data` already carried the
flow data — but the clone is **sparse** (`scripts` only), so `data/` was not in
the working tree and the data looked absent.

**Impact:** the Market engine's `fii_score` was permanently `None`, so
`risk_appetite` silently degraded to breadth-only (`market_engine.py:230-239`),
and every economic run paid ~19 s of latency for nothing.

**Fix** — vendor first, network only as fallback:

| Source | Data | Measured |
|---|---|---|
| `data/FLOWS/fii_dii_cash.parquet` | FII net, DII net (₹ Cr) | **0.07 s** |
| `data/FLOWS/fii_derivatives.parquet` | FII long ratio, index-futures net OI | 0.07 s |
| `data/INDICES/INDIA_VIX.parquet` | India VIX | 0.07 s |
| NSE JSON | fallback only | 8.9 s, empty |

Full macro stack: **19 s → 3.1 s**.

**Freshness is now explicit.** `_staleness()` compares the newest vendor row to
today in *trading* days (weekends/partial weeks handled) and sets
`meta.stale = True` beyond `max_lag_trading_days` (default 2). This is the
F-08 fix applied to the one source where a real date is available.

**Two data traps found while wiring it**

1. **Date is the row index, not a column**, in `INDICES/*.parquet`. A
   column-only lookup silently fell through to the NSE fallback — the caller saw
   a correct value and a 9 s stall.
2. **The newest bar is partial.** `INDIA_VIX` and `NIFTY_50` both end 2026-10-13
   with `Close = NaN`. The reader drops NaN/non-positive closes, so VIX reports
   **15.25** from 2026-10-12 rather than injecting NaN.

**Vendor pin is currently behind:** last flow row **2026-10-13**, last VIX
**2026-10-12** (~7 trading days). The provider reports this rather than hiding it.

**Test:** `test_fii_vendor_source.py` (13 tests) — real read, out-of-order file
ordering, staleness boundaries, corrupt file, missing dir, downstream
`fii_score`.

---

### F-22 · P3 · OPEN — Pre-existing test failure outside this scope

`test_phase4_registry_cli.py::test_list_strategies_for_ui_includes_params_space_for_ocl`
fails on a clean tree (verified by stashing every change in this audit): the OCL
`params_space` omits `min_body_pct`, although `run_pov.py:109` exposes it. Recorded
so it is not mistaken for a regression from this work.

---

## 4. Live-data check — detailed

**Command:** `python Temp/Audit/snapshot_provenance.py`
**Probe:** 2026-10-22 11:50:44 IST, `client=None`

### Source health

| Provider | ok | stale | latency | warnings |
|---|---|---|---|---|
| `cached:fii` | True | False | **18,955 ms** | 2 |
| `cached:rbi_fbil` | True | False | 2,817 ms | 0 |
| `cached:mospi` | True | False | 1,710 ms | 0 |
| `cached:india` | True | False | 30 ms | 1 |

**Observation:** the FII provider takes **19 s**. It is first in the composite
(`providers/__init__.py:104-108`), and `CompositeProvider._fetch()` queries
providers **sequentially** (`composite.py:58-80`). A cold economic run therefore
spends ~19 s of its budget on a source that, in this probe, delivered **nothing**
(`FII/DII flow unavailable`). **N-11:** run provider fetches concurrently with a
per-source timeout, or drop FII to the back of the priority list.

### Provenance classification

| Signal | Value | Class | Notes |
|---|---|---|---|
| `inflation` | 5.54 | **LIVE** | MoSPI CPI, Sept 2026 |
| `gdp_growth` | 7.82 | **LIVE** | MoSPI NAS, Q1 FY27 |
| `employment` | 4.80 | **LIVE but 10 months old** | F-08 |
| `currency` | 94.7145 | **LIVE** | RBI FBIL, 21 Oct 2026 13:00 |
| `bond_return` | 6.68 | **STATIC** | 1.0 weight |
| `debt` | 81.92 | **STATIC** | 0.5 weight |
| `forex_reserves` | 765.9 | **STATIC** | 0.5 weight |
| `news_tone` | — | **ABSENT** | no headlines in context |
| `fii_inflow` | — | **UNAVAILABLE** | provider warning |

**Static share of the composite: 2.0 / 6.0 = 33 %.**

### Weather warning (permanent operational note)

`bond_return`, `debt` and `forex_reserves` have **no live source** and the config
says so candidly (`macro_master.json:46,52,86`). Any dashboard view must label
them, or an operator will read a static 6.68 % yield as today's close.

---

## 5. Recommendations, in priority order

### Do before the next live session

| # | Item | Why |
|---|---|---|
| **N-01** | Per-signal `max_age_days`; composite sets `stale=True` beyond it (F-08) | A 10-month-old print currently scores as fresh |
| **N-02** | Weight or exclude static signals; expose `live_signals` count (F-09) | 33 % of the decision is unfalsifiable |
| **N-07** | `PovEngine` must honour `dry_run` (F-17) | One line; prevents accidental live orders |
| **N-08** | Reconcile the two entry points (`MacroPipeline` vs `runner.run_with_config`) | They disagree on what "live" means |

### Next

| # | Item | Why |
|---|---|---|
| **N-03** | Every loop `break` sets a `summary_reason` (F-10) | "Aborted" must not look like "no volume" |
| **N-05** | TTL-cache the risk context per child (F-15) | Broker round-trip in the order path |
| **N-09** | Surface ledger-reconciliation failures (F-20) | Silent under-fill |
| **N-11** | Concurrent provider fetch with timeouts (FII = 19 s) | Cold-start cost |

### Design decisions needed (not code)

| # | Question |
|---|---|
| **D-01** | Sleeves are **indices**, execution is a **stock** (F-03). Emit constituent weights, or trade the index ETF? The budget fallback is safe but blunt. |
| **D-02** | Slippage feedback needs **real fills** (F-12). Which broker feed supplies them, and what is the penalty policy? |
| **D-03** | `min_cash_buffer_pct = 0.15` caps gross at 0.85, so a RISK_ON 1.3× tilt is **always** clipped (F-05). Is 0.85 the intended ceiling, or should the buffer shrink in RISK_ON? |
| **D-04** | Is `MacroPipeline` a **batch** job (it blocks for `duration`) or a **service**? (F-16) |

---

## 6. Regression coverage

**Added:** `Blue_engine/tests/test_economic_to_pov_audit.py` — 15 tests, one per
finding cluster.

```
test_economic_multiplier_scales_the_book[0.74-…]      F-01
test_economic_multiplier_scales_the_book[1.3-0.85]    F-01/F-05
test_neutral_multiplier_is_not_reported_as_applied    F-01
test_economic_guard_reduces_execution_quantity        F-02
test_economic_guard_never_increases_quantity          F-02
test_economic_guard_inactive_without_tilt             F-02
test_slice_falls_back_to_budget_then_applies_tilt     F-02/F-03
test_slice_refuses_a_symbol_with_no_price_to_size…    F-03
test_slice_caps_at_sleeve_allocation_when_names_match F-03
test_safe_float_preserves_fractional_prices           F-04
test_safe_float_is_defined_once                       F-04
test_zero_volatility_is_reported                      F-06
test_zero_volatility_warning_is_not_duplicated        F-06
test_implausible_beta_is_reported                     F-06
test_pov_config_is_not_scaled_by_a_missing_key        F-18
```

**Run:**

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system
.venv\Scripts\python.exe -m pytest Blue_engine/tests/test_economic_to_pov_audit.py `
    Blue_engine/tests/test_portfolio_engine.py Blue_engine/tests/test_economic_engine.py `
    Blue_engine/tests/test_pov_ceiling_halt.py -q
# 168 passed
```

Full suite: **2 failures, both pre-existing** (F-22), verified by stashing all
changes and rerunning.

---

## 7. Files changed

| File | Change | Findings |
|---|---|---|
| `portfolio/portfolio_engine.py` | `_economic_tilt`, re-cap after tilt, input validation | F-01, F-05, F-06 |
| `pov/pov_engine.py` | budget fallback, economic guard, dead-code removal | F-02, F-03, F-18 |
| `pov/slicers/pov.py` | single `_safe_float`, kill-switch hook, slippage guard, squareoff guard, exact strategy match, dead code | F-04, F-10, F-11, F-12, F-13, F-19 |
| `pov/slicers/base.py` | `kill_tripped` / `halt_check` hooks | F-13 |
| `core/runner.py` | kill re-check, side-aware `reduces_exposure` | F-13, F-14 |
| `portfolio/optim_policy.py` | `peek_kill_switch()` | F-07 |
| `core/screener_bridge.py` | use non-latching read | F-07 |
| `core/pipeline.py` | **restored to original (zero diff)** — my earlier edit was based on a wrong premise | — |
| `economics/providers/fii_provider.py` | vendor parquet as primary source; staleness check; VIX from vendor | F-23 |
| `tests/test_fii_vendor_source.py` | new — 13 tests | F-23 |
| `core/registry.py` | lazy discovery; single-scan `_discover`; removed duplicate `get_risk_engine` | F-26 |
| `economics/economic_engine.py` | stale-data cap; `fii_inflow` signal + scale; `data_stale` output | F-25, F-27 |
| `market/market_engine.py` | `safe_float` guard on `fii_scale`; 20-day z-score blend | F-28 |
| `optimizer/spaces.py` | removed non-functional `min_body_pct` from the search space | F-24 |
| `input/macro_master.json` | `fii_scale` 1000→6000, z-score knobs, `fii_inflow` weight | F-25, F-28 |
| `tests/test_optimizer_data_cache.py`, `tests/test_phase4_registry_cli.py` | inverted the `min_body_pct` assertion | F-24 |

**Not committed.** Working tree only, pending your review.

---

## 7b. Findings added in the second pass (F-24 – F-28)

These were found by running the pipeline end-to-end on live data and checking that
**every engine contributed a non-empty artefact**, not merely that it returned
`ok=True`.

### F-24 · P2 · FIXED — Optimizer searched a parameter the strategy ignores

**Files:** `optimizer/spaces.py:62-69`, `alpha/signals/_pattern.py:88-97`

`spaces.py` declared `min_body_pct` as a tunable `float[0.10, 0.70]`, while
`_pattern.py` removed the body-size filter by operator instruction and now does
`_ = min_body_pct` — it accepts and **ignores** the value.

Every trial in that dimension therefore evaluated an *identical* strategy. Search
budget was spent on a flat axis, and the importance plot would report the
parameter as unimportant for a structural reason rather than a statistical one.

**Fix:** removed from the search space, with a comment tying it to the strategy
code. Two tests that asserted its presence were inverted to assert its **absence**
(`test_optimizer_data_cache.py`, `test_phase4_registry_cli.py`) — they encoded the
wrong contract. Both suites now pass (42 passed).

---

### F-25 · P1 · FIXED — `fii_inflow` was produced but never scored

**File:** `economics/economic_engine.py:68-90`, `:332-345`

`FiiFlowProvider` emits a `fii_inflow` value block, but `DEFAULT_SIGNALS` had no
entry for it. The Market engine scores FII under a **different key** (`data["fii"]`),
so the block was carried through every run and silently discarded.

**Verified before:** a macro block containing `fii_inflow = −13,051 Cr` produced a
signal list with no `fii_inflow` in it.

**Fix:** added to `DEFAULT_SIGNALS` (weight 0.5, direction +1) with unit scale
`1e11` INR (= 10,000 Cr, a large one-way institutional day). Weight is deliberately
below the other macro signals because the Market engine already carries FII in
`risk_appetite`; double-counting at full weight would overstate one flow.

**Verified after:** `fii_inflow` scores **−0.0868** for −13,051 Cr and enters the
composite.

---

### F-26 · P1 · FIXED — Importing any engine cost 47 seconds

**File:** `core/registry.py:99-152` and module tail

Three defects:

1. **`discover_all("blue_engine")` ran at import time.** `core.base` imports
   `core.registry`, so importing *any* engine triggered discovery, which imported
   every engine package — which imported `core.base` again. Measured: **47 s** for
   `import blue_engine.core.pipeline`.
2. **`_discover` was called once per registry** (4 registries × 6 packages = 24
   scans), so every module was imported and re-inspected 24 times. It also carried
   a no-op loop (`for base in registry.values(): if obj is base: continue`).
3. **`get_risk_engine` was defined twice**; the second silently shadowed the first.

**Fix:** single-scan `_discover(package, registries)`; discovery deferred to first
lookup via `ensure_discovered()`; duplicate and dead code removed.

**Residual:** import is still ~11 s because `economics/__init__` eagerly imports
`providers` (pandas/pyarrow) and `models` (scipy/sklearn). **N-12 (OPEN):** make
those sub-package `__init__` files lazy. It is a packaging change with broad blast
radius, not a one-liner, so it is documented rather than guessed at.

---

### F-27 · P0 · FIXED — Stale macro data could LEVER the book up

**File:** `economics/economic_engine.py:412-440`

**Verified before:** a macro block carrying `meta = {"ok": True, "stale": True}`
produced `regime="MILD_RISK_ON"` and a gross multiplier of **1.1016** — the engine
**increased** exposure on data the provider had explicitly flagged as stale — with
**no warning anywhere**.

That inverts the project's hierarchy (Data Integrity → Risk Control → …): stale
data may reduce risk, never increase it.

**Fix:** when `meta.stale` is true the multiplier is capped at **1.0**, a WARNING is
emitted, and `data["data_stale"] = True` is surfaced so the dashboard and
`RiskEngine` can treat the regime as advisory.

**Verified after:** same input → `mult=1.0`, `data_stale=True`, warning present.

---

### F-28 · P1 · FIXED — FII point score saturated; no context, no guard

**Files:** `market/market_engine.py:121-189`, `input/macro_master.json:102-112`

**Saturation.** `fii_scale` was **1,000 Cr**. Over 170 real vendor trading days,
`|net| >= 1,000` on **82 %** of days, so `fii_score` pinned at ±1 nearly every
session. A −1,305 Cr day and a −5,000 Cr day scored **identically**: a binary
bought/sold flag wearing a magnitude signal's clothes.

**Fix:** scale → **6,000 Cr** (~95th percentile of `|net|`). Saturation drops to
5 % of days and normal days stay mid-band: −1,305 Cr now scores **−0.218**.

**No context.** A single day of flow is noisy: +2,000 Cr is unremarkable in a week
running ±4,000 Cr but extreme in a quiet month. Added a **20-day z-score**
(`_fii_zscore`) blended with the level score at `fii_zscore_weight = 0.35`
(`0.0` reproduces the old behaviour exactly). `tanh` saturation prevents a 4σ day
from dominating. Both views are reported (`fii_point_score`, `fii_zscore`,
`fii_zscore_weight`, `fii_scale_cr`) so an operator can see which one moved risk
appetite.

**Robustness.** `scale <= 0` raised `TypeError` when the scale arrived from JSON as
a non-numeric string, and mis-divided a numeric string. Now coerced with
`safe_float` first: a bad configured scale falls back to 6,000, and a bad *direct*
scale returns `None` (breadth-only) instead of crashing.

**Live state at probe (2026-10-22):** the vendor parquet's newest flow row is
**2026-10-13**, ~6 trading days behind. With `max_lag_trading_days = 2` the
provider correctly reports `stale=True`, and F-27's cap stops that staleness from
levering the book.

---

## 7c. Engine-contribution trace (live run)

Run: `Temp/Audit/engine_contribution.py`. It checks each stage produced a
**non-empty, consumable** artefact, not merely `ok=True`.

| # | Engine | Contribution observed | Verdict |
|---|---|---|---|
| 1 | Economic | regime, score, multiplier, signals with provenance | OK |
| 2 | Market | breadth, risk_appetite, FII point + 20d z, vol state | OK |
| 3 | Portfolio | weights, gross, `economic_tilt` applied | OK |
| 4 | Alpha | candidates evaluated, selection emitted | OK |
| 5 | Risk | veto decided, `risk_assessed` flag | OK |
| 6 | POV | slice status + config + economic guard | OK |
| 7 | Infrastructure | `ready_to_trade` + per-check health | OK |

**Two integration gaps the trace exposed, still OPEN:**

* **N-13 — `risk_assessed = False`** by default: no `risk_inputs` are supplied, so
  the Risk engine performs no incident assessment. It is honest about this, but the
  pipeline therefore has **no data-driven risk veto on by default** — only the
  infrastructure gate and the pre-trade `RiskGate`.
* **N-14 — duplicate warnings**: Infrastructure runs twice (pre-flight + report),
  so each infrastructure warning appears **twice** in `report["warnings"]`.
  Cosmetic, but repetition trains operators to ignore the list.

---

## 8. Assumptions and limits

1. **No broker was contacted.** `client=None` for all probes. The live path
   (`runner.run_with_config`) was audited by **code read only** — the
   `placeorder` / `positionbook` behaviour is inferred from call sites, not observed.
2. **`positionbook()` shape** is assumed to be
   `{"data": [{"symbol", "exchange", "quantity"}]}` — the same shape the rest of
   the codebase reads (`risk_context.fetch_risk_context`).
3. **Prices are assumed decimal**, not paise.
4. Latencies are a **single sample** and should not be treated as a distribution.
5. `employment` age is computed from the `as_of` string; the PLFS release cadence
   was not independently verified.

---

*Generated 2026-10-22. Every `FIXED` finding has a test. Every `OPEN` finding is
deliberately open — a documented decision, not an oversight.*
