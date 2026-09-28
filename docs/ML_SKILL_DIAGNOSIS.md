# ML Skill Diagnosis — NIFTY, ridge baseline, h=5

**Date:** 2026-09-28
**Scope:** Why does the walk-forward report negative `skill` for every candidate?
**Verdict: NOT a bug. The feature set genuinely carries no exploitable signal
at this horizon, and the evaluation layer is correctly refusing to endorse it.**

Investigated with real cached data (NIFTY, 2,888 daily bars, 2015–2026) by four
read-only probes. Commands to reproduce are at the bottom.

---

## 1. The headline finding

| Quantity | Value |
|---|---|
| Usable rows (after feature warm-up + label tail) | 2,822 |
| Label base rate (h=5, binary, 10 bps cost) | **0.5425** |
| **Accuracy needed to score `skill > 0`** | **> 0.5425** |
| Ridge model pooled accuracy | **0.4732** |
| Ridge pooled AUC | **0.4638** |
| Mean predicted probability | 0.4957 (stdev 0.0176) |

The model predicts essentially a constant 0.496 with negligible spread. It is
**not inverted, not mis-scaled, and not leaking** — it has simply learned that
the unconditional up-rate is ~50%, and produces a nearly flat probability. On a
54.25% base rate, a flat 50/50 guess lands *below* the majority class, which is
exactly how `skill` becomes ≈ −0.10 **without any defect**.

## 2. Why `skill` is negative even though nothing is broken

`metrics.skill` measures accuracy against the **majority-class** rate:

```
always-UP    acc = 0.5425   skill = +0.0000   ← the real bar
always-DOWN  acc = 0.4575   skill = −0.0850
```

So the bar to clear is 54.25%, and a model with no edge cannot clear it — it can
only tie it (predict all-up) or lose to it (sometimes predict down). Negative
skill here means **"worse than always-long"**, which is the correct and honest
signal that this model should not be traded. It is not evidence of a code fault.

**This is the evaluation layer working as designed.** The alternative — a
plausible-looking +0.02 skill from a leaky or overfit run — is the thing the
gates exist to prevent.

## 3. Feature screen: no feature separates up from down

Every feature was screened two ways — rank AUC, and the best single-threshold
rule chosen **in-sample** (i.e. with hindsight, an upper bound):

| Feature | AUC | Best in-sample accuracy | Skill |
|---|---|---|---|
| month | 0.5200 | 0.5487 | **+0.0062** |
| hl_range | 0.5327 | 0.5421 | −0.0003 |
| vol_z_20 | 0.5048 | 0.5342 | −0.0073 |
| ret_1 | 0.5092 | 0.5305 | −0.0121 |
| mom_20 | 0.5042 | 0.5318 | −0.0101 |
| rsi_14 | 0.4888 | 0.5315 | −0.0094 |
| vol_20 | 0.4960 | 0.5349 | −0.0066 |
| ma_gap_20 | 0.4988 | 0.5286 | −0.0129 |
| … (all 13 features in the same band) | | | |

**Even with hindsight, no single feature beats the majority class by more than
0.6 percentage points.** `month` is the only positive one and it is a *calendar*
feature with no price information — a 0.6% in-sample edge on 2,822 rows is well
inside noise (roughly 1 standard error is ≈ 0.9%).

All |AUC − 0.5| values are ≤ 0.033, and the pooled model AUC of 0.4638 means the
fitted combination is *below* chance. The consistent explanation is that the
target is dominated by noise at this horizon, not that any one column is broken.

## 4. Leakage ruled out

The project's own auditor was run against the real feature builder:

```
audit_features(rows, build_features=engineering.build_price_features,
               forward_returns=fwd)
→ fatal = 0, degraded = 0
```

**Zero fatal findings.** The shift test confirms the feature builder does not
read the future. Combined with §3, this closes the "the features are silently
garbage" hypothesis: they are correct, they are just not predictive at h=5.

## 5. Fold-by-fold detail (h=5)

| Fold | n_test | base | mean p | acc | skill | AUC | long% |
|---|---|---|---|---|---|---|---|
| 0 | 470 | 0.609 | 0.4974 | 0.4809 | −0.1277 | 0.5150 | 42.6 |
| 1 | 470 | 0.553 | 0.4797 | 0.4277 | −0.1255 | 0.4138 | 34.7 |
| 2 | 470 | 0.572 | 0.5016 | 0.4468 | −0.1255 | 0.3956 | 61.1 |
| 3 | 470 | 0.577 | 0.4991 | 0.4596 | −0.1170 | 0.4736 | 41.9 |
| 4 | 470 | 0.443 | 0.5004 | 0.5511 | −0.0064 | 0.5690 | 50.0 |

Two observations that matter:

- **Folds 0–3 are all strongly up-trending** (base 0.55–0.61). The model
  predicted roughly 50/50 in each and therefore underperformed by ~12 points.
  This is a *trend-following failure*: in a rising market the unconditional
  answer ("up") is better than any of these features.
- **Fold 4 flips** (base 0.443) and the model scores near zero, with AUC 0.569.
  The instability of AUC across folds (0.40 → 0.57) is itself the signature of
  no persistent signal — a real edge would not invert for four consecutive folds
  and then appear once.

## 6. Data quality: clean

The input series was verified before any modelling:

- 2,888 bars, 2015-01-01 → 2026-09-15, no gaps or duplicated values
- mean daily return +0.0403% ≈ **10.15%/yr** simple drift (plausible for NIFTY)
- daily stdev 1.021% ≈ **16.2% annualised** vol (plausible)
- largest single move −13.0% (the March-2020 COVID crash — a real event)
- longest run of identical closes: 1 (no stale/forward-filled blocks)

**The data is a valid NIFTY series.** The negative skill is not a data artefact.

---

## Conclusions

1. **No bug.** The features are leak-free, correctly aligned, correctly scaled;
   the model trains and predicts as written. Verified by the project's own
   leakage auditor and by hand-computed benchmarks.
2. **No edge exists** in this feature set at a 5-day horizon on an index. The
   honest measured statement is: *13 standard price features do not predict the
   sign of NIFTY's next 5-day return above the 54.25% majority-class rate.*
3. **The gates worked.** `skill ≤ 0` correctly blocked a model that would have
   lost money versus simply holding long. Had the pipeline reported a flattering
   number here, that would have been the failure.

## What this implies for next steps (not done in this task)

Ranked by expected value, with the reasoning stated so each can be challenged:

1. **Change the target, not the model.** Predicting the *sign* of an index's
   5-day return is close to the hardest target available. Volatility is
   materially more predictable than direction in equity indices. Re-target at
   volatility or at a cost-aware triple-barrier label before adding any model
   complexity.
2. **Stop testing on the index alone.** The 54% up-rate *is* the signal in a
   trending index — there is little left for features to add. Cross-sectional
   prediction (rank many stocks against each other on the same date) removes the
   market-drift component and is where relative features can actually work. This
   is the standard statistical argument for cross-sectional over time-series
   targets, and it is the single largest structural change available.
3. **Fix the majority-class baseline first.** Any future run should be required
   to beat `always-UP` before other metrics are even read. That check is one
   line and should arguably live in `fold_summary`.
4. **Only then** consider GBM/CNN/RL. Per the project spec, advanced models are
   warranted only where a simple model has already shown measurable improvement.
   On this evidence there is nothing for them to improve on.

Two smaller items found while diagnosing, worth recording:

- `price_cache` fell back to `YAHOO` bars while the manifest recorded
  `sources: ['price_cache']`. The provenance string should record the exchange
  actually used (`NSE_INDEX` vs `YAHOO`) so a model's data source is not
  overstated.
- `fold_summary` reports `skill` but does not compare it against a
  **majority-class** bar. Adding that verdict would make "negative skill in a
  trending market" self-explanatory on the dashboard.

## Assumptions and limits — stated plainly

- **Daily bars only.** No intraday, order-book, flow or fundamental data was
  tested. The conclusion is limited to *these features at this horizon*.
- **One instrument (NIFTY), one period (2015–2026).** A different instrument or
  sample could differ.
- **Linear and rank methods only.** A non-linear model might find interactions
  these screens cannot see — but §3's in-sample single-feature ceiling and the
  leakage-clean bill of health make that a low-probability rescue, and the
  correct response is still "change the target" before "add capacity".
- **This is not a claim that NIFTY is unpredictable.** It is a claim that these
  13 features do not predict its 5-day sign. Those are different statements and
  the second is the one the evidence supports.

---

## Reproducing

From the repo root, using the project venv:

```powershell
cd C:\Users\KanagalakshmiM\my-trading-system
.\openalgo\.venv\Scripts\python.exe Temp\diag1_data.py        # data sanity + label balance
.\openalgo\.venv\Scripts\python.exe Temp\diag2_features.py    # single-feature AUC screen
.\openalgo\.venv\Scripts\python.exe Temp\diag3_predictions.py # what the model actually predicts
.\openalgo\.venv\Scripts\python.exe Temp\diag4_audit.py       # leakage audit + benchmarks
```

All four probes are **read-only** with respect to the repository: they read the
price cache and write nothing. They live in `Temp/` because they are diagnostics,
not production code.