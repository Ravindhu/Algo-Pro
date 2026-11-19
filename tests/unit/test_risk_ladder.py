"""Tests for the risk ladder (rules doc sections 11 and 14, plan P2).

The worked numbers are checked with the formula, not with the example's typed values.
"""
import math

import pytest

from quant.strategies.risk_ladder import (CONVICTION, LADDER, PROBE_AGGRESSIVE, PROBE_CONSERVATIVE,
                                          RiskLadder, load_cycle_settings, shares_for)

EQUITY = 100_000.0
L = 93.0


def _ladder(ceiling=0.031):
    return RiskLadder(equity=EQUITY, htf_low=L, loss_ceiling=ceiling)


def test_shares_formula_matches_section_14_example():
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, 100, L) == 14          # 100 / 7
    assert shares_for(EQUITY, PROBE_CONSERVATIVE, 95, L) == 75         # 150 / 2 (doc said 30: wrong)
    assert shares_for(EQUITY, LADDER, 94, L) == 250                    # 250 / 1


def test_carry_is_added_to_fresh_shares():
    assert shares_for(EQUITY, LADDER, 94, L, carry=69) == 69 + 250


def test_price_at_or_below_htf_low_gives_zero():
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, L, L) == 0
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, L - 1, L) == 0


def test_probe_then_conservative_then_invalidate_carries_unfilled():
    r = _ladder()
    o1 = r.aggressive_touch("OCL1", 100)
    assert o1.reason == "probe_aggressive" and o1.qty == 14
    o1c = r.conservative_touch("OCL1", 95)
    assert o1c.reason == "probe_conservative" and o1c.qty == 75
    r.record_fill(o1, 10)
    r.record_fill(o1c, 20)
    carried = r.invalidate("OCL1")
    assert carried == (14 - 10) + (75 - 20)  # 4 + 55 unfilled
    o2 = r.aggressive_touch("OCL2", 94)
    assert o2.reason == "ladder"
    assert o2.qty == carried + 250


def test_ladder_stops_after_third_ocl_and_only_pair_can_open():
    r = _ladder()
    r.aggressive_touch("OCL1", 100)
    r.invalidate("OCL1")
    r.aggressive_touch("OCL2", 97)
    r.invalidate("OCL2")
    o3 = r.aggressive_touch("OCL3", 96)
    assert o3.reason == "ladder"
    assert r.stage == "waiting_pair"
    r.invalidate("OCL3")
    assert r.aggressive_touch("OCL4", 95) is None  # no more probe-rate entries


def test_escalation_pair_gives_one_conviction_entry_then_waits_again():
    r = _ladder()
    for i, p in enumerate([100, 97, 96], start=1):
        r.aggressive_touch(f"OCL{i}", p)
        r.invalidate(f"OCL{i}")
    assert r.conservative_touch("OCL3", 95) is None  # arms the pair
    carry_before = r.carry
    assert carry_before > 0  # nothing filled, so every planned share is carried
    o4 = r.aggressive_touch("OCL4", 94)
    assert o4.reason == "conviction"
    assert o4.qty == shares_for(EQUITY, CONVICTION, 94, L, carry=carry_before)
    assert r.carry == 0  # carried shares are now inside the 0.50 % entry
    assert r.aggressive_touch("OCL5", 93.5) is None  # pair not armed again yet
    r.conservative_touch("OCL4", 93.5)
    assert r.aggressive_touch("OCL4", 93.4) is None  # same OCL cannot count as "new"


def test_pair_needs_new_ocl_after_conservative_touch():
    r = _ladder()
    for i, p in enumerate([100, 97, 96], start=1):
        r.aggressive_touch(f"OCL{i}", p)
        r.invalidate(f"OCL{i}")
    r.conservative_touch("OCL3", 95)
    assert r.aggressive_touch("OCL3", 94) is None


def test_per_cycle_loss_ceiling_refuses_over_budget_orders():
    r = _ladder(ceiling=0.001)  # 0.1 % of equity = 100 INR of planned risk at stop
    o = r.aggressive_touch("OCL1", 100)  # 14 sh x 7 = 98 INR: fits
    assert o is not None
    assert r.conservative_touch("OCL1", 95) is None  # +150 INR would go over
    assert r.planned_risk_inr() <= 0.001 * EQUITY


def test_unset_ceiling_fails_closed():
    r = RiskLadder(equity=EQUITY, htf_low=L, loss_ceiling=None)
    assert r.aggressive_touch("OCL1", 100) is None
    assert r.orders == []


def test_load_cycle_settings_missing_file_is_not_set(tmp_path):
    out = load_cycle_settings(tmp_path / "nope.yaml")
    assert out == {"target_r": None, "loss_ceiling": None}


def test_load_cycle_settings_reads_owner_values(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("cycle:\n  target_r: 2.1\n  loss_ceiling: 0.031\n", encoding="utf-8")
    out = load_cycle_settings(p)
    assert math.isclose(out["target_r"], 2.1) and math.isclose(out["loss_ceiling"], 0.031)


def test_load_cycle_settings_refuses_percent_typed_as_whole_number(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("cycle:\n  loss_ceiling: 3.1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cycle_settings(p)
