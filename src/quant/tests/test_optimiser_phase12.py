"""Hand-checked tests for the optimiser port (phases 1-2). Numbers worked out by hand.

Diagonal covariance => no correlation, so the answers are simple ratios:
- min variance: w proportional to 1/var
- ERC and inverse vol: w proportional to 1/vol (since var = vol^2)
"""
import numpy as np
import pytest

from quant.portfolio.optimiser_base import ConstraintSpec, project_to_box
from quant.portfolio.optimiser_methods import erc_weights, inverse_vol_weights
from quant.portfolio.optimiser_registry import get_optimiser, optimise, policy_for

NAMES = ["A", "B", "C"]
DIAG_COV = np.diag([0.01, 0.04, 0.02]).tolist()   # vols 0.1, 0.2, 0.1414...


def _problem(**extra):
    p = {"universe": NAMES, "cov": DIAG_COV}
    p.update(extra)
    return p


def test_min_variance_is_inverse_variance_on_diagonal_cov():
    out = optimise("min_variance", _problem())
    assert out["ok"] is True
    # 1/var = 100, 25, 50 -> total 175 -> 4/7, 1/7, 2/7
    assert out["weights"]["A"] == pytest.approx(4 / 7, abs=1e-6)
    assert out["weights"]["B"] == pytest.approx(1 / 7, abs=1e-6)
    assert out["weights"]["C"] == pytest.approx(2 / 7, abs=1e-6)


def test_inverse_vol_is_inverse_vol_proportional():
    vols = [0.1, 0.2, 0.4]
    w = inverse_vol_weights(vols)
    # 1/vol = 10, 5, 2.5 -> total 17.5 -> 4/7, 2/7, 1/7
    assert w == pytest.approx([4 / 7, 2 / 7, 1 / 7])


def test_erc_on_diagonal_matches_inverse_vol():
    # On a diagonal cov, equal risk contribution means w_i * vol_i is equal, so w ~ 1/vol.
    cov = np.diag([0.01, 0.04, 0.16]).tolist()   # vols 0.1, 0.2, 0.4
    w = erc_weights(cov, iterations=500)
    assert w == pytest.approx([4 / 7, 2 / 7, 1 / 7], abs=1e-6)


def test_max_sharpe_with_zero_excess_return_falls_back_to_min_variance():
    out = optimise("max_sharpe", _problem(mu={"A": 0.0, "B": 0.0, "C": 0.0}))
    assert out["ok"] is True
    assert out["weights"]["A"] == pytest.approx(4 / 7, abs=1e-6)


def test_projection_box_clamps_and_reports_binding():
    spec = ConstraintSpec(max_weight=0.5)
    w, diag = project_to_box({"A": 0.8, "B": 0.2}, spec)
    assert w["A"] == pytest.approx(0.5)
    assert w["B"] == pytest.approx(0.2)
    assert "box" in diag["binding"]


def test_projection_turnover_cap_blends_toward_current():
    # one-way turnover from current to target is 0.6; cap 0.1 -> blend alpha = 1/6
    spec = ConstraintSpec(max_turnover=0.1)
    w, diag = project_to_box({"A": 0.8, "B": 0.2}, spec, current={"A": 0.2, "B": 0.8})
    assert w["A"] == pytest.approx(0.3, abs=1e-6)
    assert w["B"] == pytest.approx(0.7, abs=1e-6)
    assert "turnover" in diag["binding"]


def test_unknown_optimiser_fails_closed():
    with pytest.raises(ValueError):
        get_optimiser("black_litterman")
    out = optimise("black_litterman", _problem())
    assert out["ok"] is False and out["weights"] == {}


def test_empty_universe_is_refused_not_raised():
    out = optimise("hrp", {"universe": []})
    assert out["ok"] is False


def test_regime_policy_defaults():
    assert policy_for("RISK_OFF") == {"optimiser": "min_variance", "target_vol": 0.07}
    assert policy_for("nonsense")["optimiser"] == "risk_parity"
