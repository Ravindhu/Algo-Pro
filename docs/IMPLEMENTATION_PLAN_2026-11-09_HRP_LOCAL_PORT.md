# Implementation plan: HRP moved into src/quant (decouple item 8)

Date: 2026-11-09. Owner request: "Move hrp in to src" (decouple from Blue_engine).
Scope: `src/quant/portfolio/optimizer.py` and the HRP path only. No Blue_engine edits. No live order path touched.
Reverses the wrapper choice in decision 0003 (section 3, item 2) on owner instruction.

## Facts checked in the code

- `optimizer.py` called `blue_bridge.load("portfolio.optim.hrp").hrp_weights`. This is decouple item 8 in `docs/DECOUPLING_PLAN.md`.
- Blue HRP (`Blue_engine/portfolio/optim/hrp.py`) uses its own pure-Python single-linkage, tie rules, cluster-variance floor (1e-18) and equal-weight fallback. These differ from scipy in edge cases, so the local port copies Blue's algorithm rather than the scipy reference.
- `corr_from_cov` and `vols_from_cov` (Blue `portfolio/risk/covariance.py`) are needed too.

## Phases

- **Phase A (this doc):** inventory. Done.
- **Phase B, local port:** copy Blue's HRP algorithm into `optimizer.py` with no Blue import. Keep the wrapper contract: n == 1 gives weight 1.0; a None from the algorithm raises ValueError (fail closed).
- **Phase C, parity:** `tests/parity/test_hrp_parity.py` compares local vs Blue on 40 random covariances, n = 2..8, plus degenerate cases (zero variance, empty). Runs in the sandbox through the existing `blue` fixture.
- **Phase D, keep reference test:** `src/quant/tests/test_phase0_hrp_cov.py` (scipy reference) still runs and must still pass.
- **Phase E, docs:** update decisions 0003 and 0009 (HRP now local), DECOUPLING_PLAN item 8 status, and STATUS note.
- **Phase F, verify:** run the HRP tests, parity tests and phase-2 portfolio tests. Record counts. Record any new failures.

## Rules applied

- Output must not change: parity at rel 1e-12 against Blue on the same input.
- No Blue import in `src/quant/portfolio/optimizer.py` after the change.
- Owner golden export (`scripts/ops/export_blue_goldens.py`) is still needed for owner-machine regression. Not written in this change.

## Work log

- 2026-11-09: Phase A done. Branch `feat/hrp-local-port`.
- 2026-11-09: Phases B-D done. `optimizer.py` is local (no Blue import). `tests/parity/test_hrp_parity.py`: 43 cases, all pass. `test_phase0_hrp_cov.py` (scipy reference) and `test_phase2_portfolio.py`: pass. The one skip is the existing Blue shrinkage test (needs httpx).
- 2026-11-09: Full run `pytest tests/unit tests/parity`: 1594 passed, 3 skipped, 1 failed. The failure (`test_upgrade_pin::test_repo_pin_matches_vendored_copy`) also fails on master without this change, so it is pre-existing and not HRP-related.
- Still open: owner golden export (`scripts/ops/export_blue_goldens.py`) for owner-machine regression.
