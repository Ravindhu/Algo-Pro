"""Parity: research/perf_metrics.py must match Blue_engine ml/evaluation/metrics.py.

Runs only where Blue loads (ml.evaluation.metrics needs no openalgo, so this runs in the cloud sandbox too).
"""
import random

import pytest

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.research.perf_metrics import max_drawdown, sharpe


def _blue():
    try:
        return load("ml.evaluation.metrics")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue not loadable here: {exc}")


@pytest.mark.parametrize("seed", range(20))
def test_sharpe_matches_blue(seed):
    blue = _blue()
    rnd = random.Random(seed)
    rets = [rnd.gauss(0.001, 0.02) for _ in range(rnd.randint(2, 200))]
    for ppy in (252.0, 52.0):
        assert sharpe(rets, periods_per_year=ppy) == pytest.approx(
            blue.sharpe(rets, periods_per_year=ppy), rel=1e-12, abs=1e-12)


@pytest.mark.parametrize("seed", range(20))
def test_max_drawdown_matches_blue(seed):
    blue = _blue()
    rnd = random.Random(100 + seed)
    eq = [100.0]
    for _ in range(rnd.randint(2, 200)):
        eq.append(max(eq[-1] * (1 + rnd.gauss(0.0005, 0.02)), 0.0))
    assert max_drawdown(eq) == pytest.approx(blue.max_drawdown(eq), rel=1e-12, abs=1e-12)


def test_edge_cases_match_blue():
    blue = _blue()
    for rets in ([], [0.1], [0.01, 0.01], [None, 0.02, 0.03]):
        assert sharpe(rets) == blue.sharpe(rets)
    for eq in ([], [1.0], [0.0, 0.0, 5.0], [5.0, 4.0]):
        assert max_drawdown(eq) == blue.max_drawdown(eq)
