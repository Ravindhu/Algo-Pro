# 0003: HRP stays local; shrinkage wraps Blue_engine (after a Blue fix)

Date: 2026-11-04
Stages: Covariance, shrinkage (Phase 2); Sizing and allocation (HRP optimiser, Phase 2)
Status: accepted. Update 2026-11-04: Blue HRP fixed (commit 8986b65); HRP now WRAPPED (section 3, item 2).

## Context

Two estimators in Phase 2 had conflicting implementations: HRP (`portfolio/optimizer.py` vs
Blue `portfolio/optim/hrp.py`) and Ledoit-Wolf-style shrinkage (`portfolio/covariance.py` vs
Blue `portfolio/risk/covariance.py::ledoit_wolf_cov`). The disagreement was not yet explained.

## 1. Method

- HRP reference: Lopez de Prado (2016). Single-linkage clustering on d = sqrt(0.5 (1 - corr)),
  quasi-diagonal leaf order, recursive bisection by inverse cluster variance. Implemented
  independently in `tests/unit/test_phase0_hrp_cov.py` with scipy.
- Shrinkage reference: the sample covariance at delta = 0 must equal `np.cov(ddof=1)`.
  Blue's sample covariance was checked against numpy (max diff 5e-20).

## 2. Findings

| Check | Result |
|---|---|
| `src` HRP vs reference, 25 random covariances | Exact match (max diff 0 on the first example; all 25 pass at 1e-12) |
| Blue HRP vs reference, same covariance | Differs (max 0.034). Blue's quasi-diagonal order differs from the reference. Cause not isolated |
| `src` shrinkage vs numpy at delta = 0 | Exact (by construction) |
| Blue `ledoit_wolf_cov(shrink=0)` vs numpy | NOT equal (ratio 1.23). Traced to Blue `nearest_psd` |
| Root cause of the 1.23 ratio | Blue `_jacobi_eig` returned eigenvectors as columns; `nearest_psd` read them as rows. Fixed in Blue, commit `9b0bff4`, with 4 regression tests |
| Blue shrinkage after fix vs `src`, 240 random panels | Max diff 6.5e-13 |

## 3. Decisions

1. **Shrinkage: WRAP.** `src/quant/portfolio/covariance.py::shrink_cov` delegates to Blue
   `ledoit_wolf_cov(shrink=delta)`. Our validation of `delta` stays local. Reason: after the
   Blue fix, the two agree to rounding error, so wrapping changes no numbers.
2. **HRP: WRAP (updated 2026-11-04).** The original decision kept HRP local because Blue's
   quasi-diagonal order differed from the reference. The fix in commit 8986b65 makes Blue match
   the reference: 60/60 regression cases pass (order and weights), and the 25-case src test
   passes against Blue. `src/quant/portfolio/optimizer.py::hrp_weights` now delegates to Blue,
   keeps the n == 1 shortcut, and raises ValueError if Blue returns None.
3. **Blue `nearest_psd` / `_jacobi_eig`: FIXED** (Blue commit `9b0bff4`). This was a clear
   defect with an exact reproduction, so it was fixed in place, not wrapped around. Blue
   suite failure set unchanged (49, all pre-existing).

## 4. Open for the owner

- ~~Blue HRP deviates from the published algorithm~~ Resolved in 8986b65 (quasi_diag root
  traversal). Blue's HRP feeds Blue's optimiser, so allocations in Blue's own optimiser change
  too. Owner should re-check any Blue-based backtest that used HRP.
- Blue DSR threshold (decision 0001) and PBO docstring remain open.
