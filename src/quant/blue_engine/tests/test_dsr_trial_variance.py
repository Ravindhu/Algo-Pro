"""Deflated Sharpe: the selection threshold must scale with the cross-trial Sharpe variance.

Reference: Bailey & Lopez de Prado (2014). E[max SR] = sqrt(V) * ((1-g) Z^-1(1-1/N) + g Z^-1(1-1/(N e))).
Blue originally fixed V = 1, which over-states the threshold. The optional `trial_sharpe_var`
argument fixes it. Without it the legacy result must be unchanged.
"""
import math

import pytest

from blue_engine.ml.evaluation.metrics import deflated_sharpe


def _reference_emax(n, v):
    from scipy.stats import norm
    g = 0.5772156649
    return math.sqrt(v) * ((1 - g) * norm.ppf(1 - 1 / n) + g * norm.ppf(1 - 1 / (n * math.e)))


@pytest.mark.parametrize("n,v", [(10, 0.01), (100, 0.01), (100, 0.0025), (300, 0.04)])
def test_expected_max_scales_with_trial_variance(n, v):
    out = deflated_sharpe(0.2, n_trials=n, n_obs=500, trial_sharpe_var=v)
    assert out["expected_max_sharpe"] == pytest.approx(_reference_emax(n, v), abs=1e-4)
    assert out["trial_sharpe_var_used"] == v


def test_zero_trial_variance_removes_selection_premium():
    out = deflated_sharpe(0.2, n_trials=100, n_obs=500, trial_sharpe_var=0.0)
    assert out["expected_max_sharpe"] == 0.0
    assert out["prob_real"] > 0.99


def test_legacy_call_is_unchanged_and_records_assumption():
    legacy = deflated_sharpe(0.2, n_trials=100, n_obs=500)
    explicit = deflated_sharpe(0.2, n_trials=100, n_obs=500, trial_sharpe_var=1.0)
    assert legacy["expected_max_sharpe"] == explicit["expected_max_sharpe"]
    assert legacy["trial_sharpe_var_used"] == 1.0


def test_negative_variance_rejected():
    with pytest.raises(ValueError):
        deflated_sharpe(0.2, n_trials=10, n_obs=100, trial_sharpe_var=-0.1)
