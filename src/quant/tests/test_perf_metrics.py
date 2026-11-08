"""Hand-checked values for research/perf_metrics.py (run everywhere, no Blue needed)."""
import math

import pytest

from quant.research.perf_metrics import max_drawdown, sharpe


def test_sharpe_hand_checked():
    # mean 0.02, sample sd sqrt(0.0002) = 0.014142..., ratio sqrt(2) before annualising
    got = sharpe([0.01, 0.03], periods_per_year=252)
    assert got == pytest.approx(math.sqrt(2) * math.sqrt(252))


def test_sharpe_undefined_cases():
    assert sharpe([0.01]) is None
    assert sharpe([0.01, 0.01, 0.01]) is None      # zero spread
    assert sharpe([None, 0.01]) is None            # None is dropped, then only one value


def test_sharpe_risk_free_lowers_result():
    base = sharpe([0.01, 0.03, 0.02, 0.04])
    with_rf = sharpe([0.01, 0.03, 0.02, 0.04], risk_free=0.005)
    assert with_rf < base


def test_max_drawdown_hand_checked():
    # peak 120, trough 90 -> (120 - 90) / 120 = 0.25
    assert max_drawdown([100, 120, 90, 110]) == pytest.approx(0.25)


def test_max_drawdown_undefined_and_no_fall():
    assert max_drawdown([100]) is None
    assert max_drawdown([100, 110, 120]) == 0.0


def test_max_drawdown_skips_non_positive_peaks():
    # peak 0 is skipped; no division by zero
    assert max_drawdown([0, 0, 5]) == 0.0
