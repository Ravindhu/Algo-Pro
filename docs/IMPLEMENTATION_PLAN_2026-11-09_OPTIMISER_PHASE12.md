# Implementation plan: optimiser port, phases 1 and 2 (decouple stage 6.4)

Date: 2026-11-09. Owner request: "Phase 1 + 2" (core optimisers, then NCO).
Scope: `src/quant/portfolio/` only. No Blue_engine edits. No live order path touched.
Out of scope (owner later): Black-Litterman, min CVaR, benchmark.py, significance.py.

## Facts checked in the code

- Blue optimiser base (`portfolio/optim/base.py`): ConstraintSpec, BaseOptimiser (never raises),
  box/gross/beta/turnover projection, helpers.
- Methods in scope: `mean_variance.py` (min variance, max Sharpe), `risk_parity.py` (ERC,
  inverse vol), `hrp.py` (HRP class, NCO class), `registry.py` (names, REGIME_POLICY).
- Blue dependencies to replace locally: `risk/beta._solve` (Gaussian elimination),
  `core/results` (safe_float, clamp), `risk/covariance.corr_from_cov` (already local in optimizer.py).
- Quant code calls only `hrp_weights` today (optimizer.py). Nothing else in quant uses the optimiser yet.

## Phases

- **Phase A (this doc):** inventory. Done.
- **Phase B, base:** `src/quant/portfolio/optimiser_base.py` (ConstraintSpec, BaseOptimiser, projection and repairs, local safe_float and clamp).
- **Phase C, methods:** `src/quant/portfolio/optimiser_methods.py` (solver, min variance, max Sharpe, ERC, inverse vol, HRP class, NCO).
- **Phase D, registry:** `src/quant/portfolio/optimiser_registry.py` (names and REGIME_POLICY). Unknown names raise ValueError (Blue falls back to risk_parity silently; quant refuses instead, fail closed). Recorded as a deliberate difference.
- **Phase E, tests:** hand-checked unit tests (diagonal covariance: GMV weights proportional to 1/var; ERC and inverse vol proportional to 1/vol) and parity tests against Blue (`tests/parity/test_optimiser_parity.py`).
- **Phase F, verify:** run the new tests, the HRP tests, and the unit + parity suites. Record counts.
- **Phase G, docs:** DECOUPLING_PLAN stage 6.4 status, flow line, decision note.

## Rules applied

- Output must match Blue at rel 1e-12 (same algorithm, same tie and floor rules).
- No Blue import in the new modules.
- Fail closed: an optimiser that fails returns ok=False (as Blue does); never raises.

## Work log

- 2026-11-09: Phase A done. Branch `feat/optimizer-phase1-2`.
- 2026-11-09: Phases B-E done. Base, methods, registry written with no Blue import. Tests: 9 hand-checked, 82 parity (72 method + 10 projection), all pass.
- 2026-11-09: Phase F: `pytest tests/unit tests/parity src/quant/tests`: 2086 passed, 16 skipped, 1 xfailed, 1 failed. The failure is `test_upgrade_pin` (pre-existing on master).
- Phase G done (DECOUPLING_PLAN 6.4 status).
