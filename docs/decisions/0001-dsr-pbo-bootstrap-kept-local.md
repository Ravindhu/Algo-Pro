# 0001: Keep deflated Sharpe, PBO and bootstrap in `src/quant/research/validation.py`

Date: 2026-11-03
Stage: Trial logger + DSR/PBO, Bootstrap of trades (Phase 1)
Status: accepted. Blue_engine change is a separate decision for the owner.

## Context

AGENTS.md says `src/quant/` must wrap `Blue_engine/`, never copy it. The Phase 0 audit
checked the four functions in `validation.py` against Blue_engine. Three of them do not
have a wrap-safe equivalent.

## Decision

| Function | Blue_engine equivalent | Decision |
|---|---|---|
| `sharpe` | `ml/evaluation/metrics.py::sharpe` | WRAP (done). Same formula. 0.0 kept for undefined input. |
| `deflated_sharpe` | `ml/evaluation/metrics.py::deflated_sharpe` | KEEP local. See evidence below. |
| `prob_backtest_overfit` (PBO) | none | KEEP. Nothing to wrap. |
| `bootstrap_mean_pvalue` | `portfolio/optim/significance.py::bootstrap_metric`, `bootstrap_ci` | KEEP. Different output. |

## Evidence: deflated Sharpe

Bailey and López de Prado: the expected best Sharpe from N unskilled trials is
`sqrt(V) * E_max(N)`, where `V` is the variance of the trial Sharpe ratios.

- `validation.py` uses that form. It takes `trial_sharpe_var` as an input.
- Blue_engine's `deflated_sharpe` uses `E_max(N)` alone. That means `V = 1`, in per-period units.

Test on the same synthetic series (T = 500 daily returns, `V = 1/T`):

| Case | N | Local (`validation.py`) prob | Blue prob | Blue E[max] (per-period) |
|---|---|---|---|---|
| edge, per-period SR 0.076 | 10 | 0.553 | 0.000 | 1.575 |
| edge, per-period SR 0.076 | 100 | 0.206 | 0.000 | 2.531 |
| noise, per-period SR -0.017 | 10 | 0.025 | 0.000 | 1.575 |
| noise, per-period SR -0.017 | 100 | 0.002 | 0.000 | 2.531 |

At N = 1 both agree (0.956 and 0.350). From N = 10 on, Blue's threshold sits far above any
realistic per-period Sharpe. A realistic daily Sharpe is around 0.05 to 0.1. So Blue's
version rejects every strategy once more than a few variants were tried.

Wrapping Blue here would bring that scale error into the Phase 1 gate. So the local
version stays until Blue_engine is corrected.

## Evidence: PBO

`Blue_engine/ml/__init__.py` says `ml/evaluation/` includes "PBO". A search of
`Blue_engine/` (non-test code) finds no PBO implementation. The docstring is out of date.

## Evidence: bootstrap

Blue's bootstrap returns confidence intervals (`ci_low`, `ci_high`, `se`).
The local function returns a one-sided p-value that the mean trade return is above zero.
Those are different outputs. Keep the local function.

## Follow-ups (owner decision needed)

1. Blue_engine `deflated_sharpe`: add the trial-variance input, or make the threshold
   V-scaled as in the paper. Then re-run the Blue tests. Until then, do not use Blue's
   DSR in any gate.
2. Blue_engine `ml/__init__.py` docstring: remove "PBO", or implement it.
3. Consider moving the local PBO into Blue_engine later, so the whole stack lives in one place.

## Follow-up (2026-11-04): Blue DSR threshold fixed

Blue `ml/evaluation/metrics.py::deflated_sharpe` used a cross-trial Sharpe variance of 1.0
(hard-coded). The Bailey and Lopez de Prado threshold scales with its square root.
Fix: optional `trial_sharpe_var`. When it is given, the threshold uses sqrt(V). When it is
omitted, the legacy result is kept and the output field `trial_sharpe_var_used` shows 1.0.
Regression tests: `Blue_engine/tests/test_dsr_trial_variance.py` (4 tests). Blue suite
failure set unchanged (49 pre-existing).

Callers still on the legacy default (owner to decide whether to pass a real variance):
- `Blue_engine/backtest/metrics.py:227` (backtest gate)
- `Blue_engine/optimizer/study.py:304`
- `Blue_engine/ml/evaluation/walk_forward.py:387`

Our local `src/quant/research/validation.py::deflated_sharpe` already takes the variance
explicitly and stays local (this decision).
