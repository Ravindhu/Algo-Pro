# 0009. HRP optimizer: keep our implementation or switch to Blue's (SUPERSEDED by 0003)

Date: 2026-11-05
Status: SUPERSEDED by `0003-hrp-and-shrinkage.md` (accepted 2026-11-04). Blue HRP was fixed in commit 8986b65 and now matches the reference. Decision: HRP stays WRAPPED around Blue. No local port. Owner chose option 1 on 2026-11-09 (keep the 0003 wrapper).
Tracks: X-25 in `docs/IMPLEMENTATION_PLAN.md`.

## Facts (checked in the repo)

- Our HRP: `src/quant/portfolio/optimizer.py`. Matches the published Lopez de Prado algorithm on 25 random covariance matrices (Phase 0 audit, `docs/audits/PHASE0_REUSE_AUDIT.md`).
- Blue HRP: `Blue_engine/portfolio/optim/hrp.py`. On the same covariance it gives different weights from ours.
- Phase 2 exit checks depend on this choice (`docs/IMPLEMENTATION_PLAN.md`, Phase 2).

## Options

A. Keep our HRP in `src/quant/portfolio/optimizer.py`. Blue HRP stays as is. Record the difference in a test that fails if Blue's output changes unexpectedly.
- Pro: already matches the published algorithm on the checks run so far.
- Con: two HRP implementations exist. The reuse rule (wrap, do not copy) is not fully met.

B. Fix Blue's HRP to match the published algorithm, then wrap it from `src/quant/portfolio/optimizer.py`.
- Pro: one implementation, follows the reuse rule.
- Con: changes a Blue module. Needs an owner test run of `Blue_engine/tests` (the sandbox cannot install `openalgo`).

## Evidence needed before choosing

- The exact difference between Blue HRP and ours on real NSE return data (not random covariances). Needs owner data (X-10).
- Which Blue callers depend on the current HRP output.

## Owner choice

Owner chose option 1 (wrapper) on 2026-11-09, then reversed it the same day: owner moved HRP into src (local port, `optimizer.py`). Blue is not changed. Final state: HRP local, see 0003 update of 2026-11-09.
