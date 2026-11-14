"""Stage 5 (Sizing and portfolio): allocator, margin, funds, Kelly, rebalance.

The allocator used to STRAND budget whenever a strategy hit its capacity, and
margin_ok approved unlimited notional when the requirement was zero. Both are
capital-allocation defects: one leaves money idle, the other lets through an
order that cannot be margined.
"""
import math

import pytest

from quant.portfolio.allocator import (allocate, allocate_report,
                                       funds_available_t1, margin_ok)
from quant.portfolio.rebalance import drift, rebalance_trades
from quant.portfolio.sizing import (fractional_kelly, risk_budget_qty,
                                    vol_target_scale)


# --------------------------------------------------------------------------- #
# allocator: redistribution (the stranded-budget defect)
# --------------------------------------------------------------------------- #
def test_equal_scores_split_the_budget():
    out = allocate(1000.0, {"A": 1.0, "B": 1.0}, {"A": 1e9, "B": 1e9})
    assert out == {"A": 500.0, "B": 500.0}


def test_capped_budget_is_redistributed_not_stranded():
    """The old code deployed only 600 of 1000 and left 400 idle."""
    out = allocate(1000.0, {"A": 1.0, "B": 1.0}, {"A": 100.0, "B": 1000.0})
    assert out["A"] == pytest.approx(100.0)
    assert out["B"] == pytest.approx(900.0)
    assert sum(out.values()) == pytest.approx(1000.0)


def test_all_strategies_capped_leaves_the_rest_unallocated():
    out = allocate(1000.0, {"A": 1.0, "B": 1.0}, {"A": 100.0, "B": 100.0})
    assert sum(out.values()) == pytest.approx(200.0)


def test_missing_capacity_means_no_ceiling():
    """An omitted key is missing information, not a zero cap."""
    assert allocate(1000.0, {"A": 1.0}, {}) == {"A": 1000.0}


def test_negative_score_receives_nothing():
    out = allocate(1000.0, {"A": -5.0, "B": 1.0}, {})
    assert out["A"] == 0.0 and out["B"] == pytest.approx(1000.0)


def test_zero_scores_allocate_nothing():
    assert allocate(1000.0, {"A": 0.0, "B": 0.0}, {}) == {"A": 0.0, "B": 0.0}


def test_non_positive_budget_allocates_nothing():
    assert allocate(0.0, {"A": 1.0}, {}) == {"A": 0.0}
    assert allocate(-5.0, {"A": 1.0}, {}) == {"A": 0.0}


def test_negative_capacity_is_refused():
    with pytest.raises(ValueError, match="capacity"):
        allocate(1000.0, {"A": 1.0}, {"A": -50.0})


def test_allocation_never_exceeds_capacity():
    out = allocate(1000.0, {"A": 1.0, "B": 1.0, "C": 1.0},
                   {"A": 50.0, "B": 75.0, "C": 1e9})
    assert out["A"] <= 50.0 + 1e-9
    assert out["B"] <= 75.0 + 1e-9


def test_report_shows_what_was_deployed_and_what_was_capped():
    rep = allocate_report(1000.0, {"A": 1.0, "B": 1.0}, {"A": 100.0, "B": 1000.0})
    assert rep["deployed"] == pytest.approx(1000.0)
    assert rep["unallocated"] == pytest.approx(0.0)
    assert rep["capped"] == ["A"]


# --------------------------------------------------------------------------- #
# margin and funds (fail closed)
# --------------------------------------------------------------------------- #
def test_margin_zero_requirement_is_refused():
    """It used to approve unlimited notional: a missing requirement, not a free one."""
    assert margin_ok(1_000_000_000, 0.0, 0.0) is False


def test_margin_within_available_passes():
    assert margin_ok(1000.0, 0.2, 250.0) is True


def test_margin_beyond_available_fails():
    assert margin_ok(1000.0, 0.2, 150.0) is False


def test_margin_rejects_a_requirement_above_one():
    assert margin_ok(100.0, 1.5, 1000.0) is False


def test_margin_rejects_non_finite_inputs():
    assert margin_ok(float("inf"), 0.2, 1000.0) is False
    assert margin_ok(100.0, float("nan"), 1000.0) is False


def test_margin_rejects_negative_availability():
    assert margin_ok(100.0, 0.2, -5.0) is False


def test_funds_never_go_negative():
    assert funds_available_t1(-100.0, 0.0, 0.0) == 0.0


def test_funds_are_settled_plus_t1_minus_reserved():
    assert funds_available_t1(1000.0, 200.0, 300.0) == pytest.approx(900.0)


def test_funds_reject_non_finite():
    assert funds_available_t1(float("nan"), 0.0, 0.0) == 0.0


# --------------------------------------------------------------------------- #
# Kelly sizing (parity-tested locally; verify the caps still hold)
# --------------------------------------------------------------------------- #
def test_fractional_kelly_is_capped():
    assert fractional_kelly(mu=10.0, sigma=0.01, fraction=0.5, cap=0.10) == pytest.approx(0.10)


def test_fractional_kelly_floors_at_zero_for_a_negative_edge():
    assert fractional_kelly(mu=-1.0, sigma=0.2) == 0.0


def test_fractional_kelly_zero_sigma_is_zero():
    assert fractional_kelly(mu=1.0, sigma=0.0) == 0.0


def test_vol_target_scale_is_bounded():
    assert vol_target_scale(0.10, 0.20, max_leverage=1.0) == pytest.approx(1.0)
    assert vol_target_scale(0.40, 0.10, max_leverage=1.0) == pytest.approx(0.25)


def test_vol_target_zero_vol_is_zero_scale():
    assert vol_target_scale(0.0, 0.20) == 0.0


def test_risk_budget_qty_rounds_down_to_lots():
    # risk 1% of 100000 = 1000; per-share risk 10 -> 100 shares; lot 25 -> 100
    assert risk_budget_qty(100_000, 0.01, 100.0, 90.0, lot_size=25) == 100


def test_risk_budget_qty_is_zero_without_a_stop_distance():
    assert risk_budget_qty(100_000, 0.01, 100.0, 100.0) == 0


def test_risk_budget_qty_is_zero_for_bad_inputs():
    assert risk_budget_qty(0, 0.01, 100.0, 90.0) == 0
    assert risk_budget_qty(100_000, 0.0, 100.0, 90.0) == 0


# --------------------------------------------------------------------------- #
# drift and rebalance bands
# --------------------------------------------------------------------------- #
def test_drift_is_current_minus_target():
    assert drift({"A": 0.6}, {"A": 0.5})["A"] == pytest.approx(0.1)


def test_inside_the_band_no_trade():
    assert rebalance_trades({"A": 0.51}, {"A": 0.50}, band=0.02) == {}


def test_outside_the_band_trades_back_to_target():
    out = rebalance_trades({"A": 0.60}, {"A": 0.50}, band=0.02)
    assert out["A"] == pytest.approx(-0.10)


def test_boundary_is_strictly_greater():
    """Blue uses >=; this keeps > so a name exactly ON the band does not trade.

    Uses a pair whose difference is EXACT in binary floating point. 0.75 - 0.70 is
    not (it is 0.050000000000000044, which triggers ">"), and 0.875 - 0.75 is
    exactly 0.125, so this measures the band rule and not float noise.
    """
    assert rebalance_trades({"A": 0.875}, {"A": 0.75}, band=0.125) == {}


def test_new_name_is_bought():
    out = rebalance_trades({}, {"B": 0.10}, band=0.02)
    assert out["B"] == pytest.approx(0.10)


def test_dropped_name_is_sold():
    out = rebalance_trades({"A": 0.10}, {}, band=0.02)
    assert out["A"] == pytest.approx(-0.10)
