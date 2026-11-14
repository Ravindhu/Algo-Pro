"""Stage 4 (Signal and conviction): calibration, meta-labeler, liquidity tiers.

These sit on the SIZE path, so a defect loses money directly rather than
corrupting a backtest. Every fix here is pinned by a test that reproduced the
original failure first.
"""
import numpy as np
import pytest

from quant.data import liquidity_profile as lp
from quant.models.calibration import (CalibrationError, fit_logistic,
                                      predict_proba, reliability, sigmoid)
from quant.models.conviction import CONDITIONS, score_conviction
from quant.models.meta_label import MetaLabelError, MetaLabeler


# --------------------------------------------------------------------------- #
# calibration: numerical safety on the size path
# --------------------------------------------------------------------------- #
def test_sigmoid_is_stable_far_negative():
    """The naive form overflows below about -709 and warns while returning junk."""
    with np.errstate(over="raise"):
        assert sigmoid(-1000.0) == pytest.approx(0.0, abs=1e-12)


def test_sigmoid_is_stable_far_positive():
    assert sigmoid(1000.0) == pytest.approx(1.0)


def test_sigmoid_midpoint():
    assert sigmoid(0.0) == pytest.approx(0.5)


def test_fit_refuses_a_single_class():
    """One class carries no information, yet a confident model would be produced."""
    with pytest.raises(CalibrationError, match="single class"):
        fit_logistic([[1.0], [2.0]], [1.0, 1.0])


def test_fit_refuses_nan_features():
    with pytest.raises(CalibrationError, match="NaN"):
        fit_logistic([[1.0], [float("nan")]], [0.0, 1.0])


def test_predict_refuses_nan_features():
    with pytest.raises(CalibrationError, match="NaN"):
        predict_proba([[float("nan")]], np.array([1.0]), 0.0)


def test_predict_refuses_a_width_mismatch():
    with pytest.raises(CalibrationError, match="width"):
        predict_proba([[1.0, 2.0]], np.array([1.0]), 0.0)


def test_fit_learns_a_separable_problem():
    X = np.array([[1.0], [2.0], [3.0], [4.0], [5.0], [6.0]])
    y = np.array([0, 0, 0, 1, 1, 1], dtype=float)
    w, b = fit_logistic(X, y)
    assert predict_proba([[6.0]], w, b)[0] > predict_proba([[1.0]], w, b)[0]


def test_reliability_omits_empty_bins():
    rows = reliability([0.1, 0.1], [0, 1], bins=5)
    assert all(count > 0 for _, _, count in rows)


# --------------------------------------------------------------------------- #
# meta-labeler: the four money-path defects
# --------------------------------------------------------------------------- #
def _labeler(**over):
    base = dict(w=np.array([1.0]), b=0.0)
    base.update(over)
    return MetaLabeler(**base)


def test_negative_max_bet_is_refused():
    """It used to return a NEGATIVE bet, which reverses the trade."""
    with pytest.raises(MetaLabelError, match="max_bet"):
        _labeler(max_bet=-0.5)


def test_zero_max_bet_is_refused():
    with pytest.raises(MetaLabelError, match="max_bet"):
        _labeler(max_bet=0.0)


def test_threshold_at_or_above_one_is_refused():
    """A threshold of 1 would silently skip every signal forever."""
    with pytest.raises(MetaLabelError, match="take_threshold"):
        _labeler(take_threshold=1.5)


def test_nan_feature_raises_instead_of_silently_skipping():
    with pytest.raises(MetaLabelError):
        _labeler().decide([float("nan")])


def test_infinite_feature_no_longer_returns_the_max_bet():
    """sigmoid(inf)=1 used to saturate to the LARGEST possible position."""
    with pytest.raises(MetaLabelError):
        _labeler().decide([float("inf")])


def test_fit_on_single_class_is_refused():
    with pytest.raises(CalibrationError):
        MetaLabeler.fit([[1.0], [2.0]], [1.0, 1.0])


def test_normal_path_is_unchanged():
    take, bet = _labeler().decide([100.0])
    assert take is True
    assert bet == pytest.approx(0.10)      # capped by max_bet


def test_bet_is_zero_when_not_taken():
    take, bet = _labeler(w=np.array([-1.0])).decide([10.0])
    assert take is False and bet == 0.0


def test_bet_never_exceeds_the_cap():
    for x in (50.0, 100.0, 500.0):
        _, bet = _labeler(max_bet=0.05).decide([x])
        assert 0.0 <= bet <= 0.05


def test_kelly_form_at_half_probability_is_zero():
    """f = 2p-1 is 0 at p=0.5, so an edge-less signal cannot be sized."""
    m = _labeler(w=np.array([0.0]), b=0.0, take_threshold=0.4)
    take, bet = m.decide([0.0])
    assert take is True and bet == 0.0


# --------------------------------------------------------------------------- #
# conviction: verify the existing fail-closed rules still hold
# --------------------------------------------------------------------------- #
def _all(value=0.8):
    return {name: value for name in CONDITIONS}


def test_conviction_scores_a_strong_setup():
    assert score_conviction(_all()).score == pytest.approx(0.8)


def test_missing_condition_forces_zero():
    """Fail closed: missing data must never add conviction."""
    conditions = _all()
    conditions["catalyst"] = None
    result = score_conviction(conditions)
    assert result.score == 0.0
    assert "catalyst" in result.missing


def test_zero_condition_forces_zero():
    conditions = _all()
    conditions["liquidity"] = 0.0
    assert score_conviction(conditions).score == 0.0


def test_out_of_range_condition_raises():
    conditions = _all()
    conditions["momentum"] = 1.5
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        score_conviction(conditions)


def test_unknown_condition_raises():
    with pytest.raises(ValueError, match="unknown condition"):
        score_conviction({**_all(), "made_up": 0.5})


def test_geometric_mean_punishes_one_weak_link():
    """One weak factor must pull the score well below an arithmetic average."""
    conditions = _all(0.9)
    conditions["execution"] = 0.2
    score = score_conviction(conditions).score
    assert score < 0.9 * 0.85          # far below the arithmetic mean


# --------------------------------------------------------------------------- #
# liquidity tiers (Stage 4.1, newly wrapped)
# --------------------------------------------------------------------------- #
def test_tier_table_is_ordered_strongest_first():
    table = lp.tier_table()
    assert table
    assert table[0]["tier"] == "MEGA"


def test_unknown_liquidity_resolves_to_micro():
    """Treating an unknown name as liquid is how a book cannot exit."""
    assert lp.tier_of(None, 100) == "MICRO"
    assert lp.tier_of(0, 100) == "MICRO"


def test_tier_scales_with_traded_value():
    assert lp.tier_of(10_000_000, 1000) == "MEGA"
    assert lp.tier_of(100, 10) == "MICRO"


def test_assess_rejects_an_illiquid_name():
    out = lp.assess("X", 100, 10)
    assert out["passes"] is False
    assert out["tier"] == "MICRO"


def test_assess_passes_a_liquid_name():
    out = lp.assess("Y", 5_000_000, 1000)
    assert out["passes"] is True


def test_assess_min_tier_is_enforced():
    out = lp.assess("Z", 500_000, 500, min_tier="LARGE")
    assert out["passes"] is False


def test_assess_rejects_a_bad_min_tier():
    with pytest.raises(ValueError, match="min_tier"):
        lp.assess("Z", 100, 100, min_tier="HUGE")


def test_profile_returns_a_dict_or_none():
    out = lp.profile("RELIANCE", "NSE", 1_000_000, 2500.0)
    assert out is None or isinstance(out, dict)
