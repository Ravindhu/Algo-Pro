"""Unit tests for the conviction score (Priority A, step A1).

These tests cover the maths and the fail-closed rules only. They do not test
profitability. Profitability is tested later, out-of-sample, in step A4.
"""
import math

import pytest

from quant.models.conviction import CONDITIONS, score_conviction


def _all(value):
    return {name: value for name in CONDITIONS}


def test_all_conditions_perfect_gives_score_one():
    result = score_conviction(_all(1.0))
    assert result.score == pytest.approx(1.0)
    assert result.missing == ()
    assert result.zeroed == ()


def test_equal_inputs_return_that_input_value():
    # Weighted geometric mean of equal values equals that value.
    result = score_conviction(_all(0.6))
    assert result.score == pytest.approx(0.6)


def test_geometric_mean_matches_hand_calculation():
    values = {"momentum": 0.9, "trend_structure": 0.8, "catalyst": 0.5,
              "liquidity": 0.7, "execution": 1.0, "strategy_history": 0.6}
    expected = math.prod(values.values()) ** (1 / 6)  # equal weights
    result = score_conviction(values)
    assert result.score == pytest.approx(expected, rel=1e-12)


def test_weights_are_normalised_so_scale_does_not_matter():
    values = _all(0.7)
    w_small = {name: 1.0 for name in CONDITIONS}
    w_big = {name: 50.0 for name in CONDITIONS}
    assert score_conviction(values, w_small).score == pytest.approx(
        score_conviction(values, w_big).score)
    assert sum(score_conviction(values, w_big).weights.values()) == pytest.approx(1.0)


def test_one_weak_condition_pulls_score_down_harder_than_arithmetic_mean():
    # Five strong conditions (0.9) and one weak one (0.1).
    values = _all(0.9)
    values["catalyst"] = 0.1
    arithmetic_mean = (5 * 0.9 + 0.1) / 6  # 0.766...
    geometric = score_conviction(values).score
    assert geometric < arithmetic_mean
    assert geometric == pytest.approx((0.9 ** 5 * 0.1) ** (1 / 6))


def test_zero_condition_forces_score_to_zero_and_is_reported():
    values = _all(0.95)
    values["liquidity"] = 0.0
    result = score_conviction(values)
    assert result.score == 0.0
    assert result.zeroed == ("liquidity",)
    assert result.missing == ()


def test_missing_none_counts_as_zero_and_fails_closed():
    values = _all(0.9)
    values["execution"] = None
    result = score_conviction(values)
    assert result.score == 0.0
    assert result.missing == ("execution",)
    assert result.components["execution"] == 0.0


def test_missing_nan_counts_as_missing_and_fails_closed():
    values = _all(0.9)
    values["momentum"] = float("nan")
    result = score_conviction(values)
    assert result.score == 0.0
    assert result.missing == ("momentum",)


def test_absent_key_counts_as_missing():
    values = _all(0.9)
    del values["strategy_history"]
    result = score_conviction(values)
    assert result.score == 0.0
    assert result.missing == ("strategy_history",)


def test_missing_condition_never_raises_log_of_zero():
    # Regression: a missing condition is stored as 0.0; it must not reach log(0).
    values = {name: None for name in CONDITIONS}
    assert score_conviction(values).score == 0.0


def test_raising_any_single_condition_never_lowers_score():
    base = _all(0.5)
    base_score = score_conviction(base).score
    for name in CONDITIONS:
        higher = dict(base)
        higher[name] = 0.8
        assert score_conviction(higher).score >= base_score


def test_out_of_range_value_raises():
    with pytest.raises(ValueError):
        score_conviction({**_all(0.5), "momentum": 1.2})
    with pytest.raises(ValueError):
        score_conviction({**_all(0.5), "momentum": -0.1})


def test_unknown_condition_name_raises():
    with pytest.raises(ValueError):
        score_conviction({**_all(0.5), "vibes": 0.9})


def test_unknown_weight_name_raises():
    with pytest.raises(ValueError):
        score_conviction(_all(0.5), {"vibes": 1.0})


def test_negative_or_nan_weight_raises():
    with pytest.raises(ValueError):
        score_conviction(_all(0.5), {"momentum": -1.0})
    with pytest.raises(ValueError):
        score_conviction(_all(0.5), {"momentum": float("nan")})


def test_all_zero_weights_raise():
    with pytest.raises(ValueError):
        score_conviction(_all(0.5), {name: 0.0 for name in CONDITIONS})


def test_result_is_deterministic():
    values = _all(0.73)
    assert score_conviction(values).score == score_conviction(values).score


def test_score_stays_in_unit_interval_at_boundaries():
    for v in (0.0, 1e-9, 0.5, 1.0 - 1e-9, 1.0):
        s = score_conviction(_all(v)).score
        assert 0.0 <= s <= 1.0
