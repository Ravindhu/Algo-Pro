"""Tests for the risk ladder (rules doc sections 11 and 14, plan P2).

The worked numbers are checked with the formula, not with the example's typed values.
"""
import math

import pytest

from quant.strategies.risk_ladder import (CONVICTION, LADDER, PROBE_AGGRESSIVE, PROBE_CONSERVATIVE,
                                          RiskLadder, load_cycle_settings, shares_for)

EQUITY = 100_000.0
L = 93.0


def _ladder(ceiling=0.05):  # wide, so mechanics tests are not cut by the owner's 1 % budget
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


def test_owner_one_percent_ceiling_blocks_the_next_order():
    r = _ladder(ceiling=0.01)  # owner value: 1 % of 1,00,000 = 1000 INR planned risk at stop
    assert r.aggressive_touch("OCL1", 100) is not None       # 14 sh x 7 = 98 INR
    assert r.conservative_touch("OCL1", 95) is not None      # 75 sh x 2 = 150 INR; total 248
    r.invalidate("OCL1")                                     # unfilled shares are carried forward
    o2 = r.aggressive_touch("OCL2", 94)                      # carry + 250 sh x 1 = risk added
    assert o2 is not None
    r.invalidate("OCL2")
    carry = r.carry
    assert carry > 0
    # OCL3 at 93.5: carry + floor(250 / 0.5) shares = carry + 500 sh, risk = (carry + 500) x 0.5
    # that pushes planned risk over the 1 % budget, so it is refused
    assert r.aggressive_touch("OCL3", 93.5) is None
    assert r.planned_risk_inr() <= 0.01 * EQUITY


def test_unset_ceiling_fails_closed():
    r = RiskLadder(equity=EQUITY, htf_low=L, loss_ceiling=None)
    assert r.aggressive_touch("OCL1", 100) is None
    assert r.orders == []


def test_load_cycle_settings_missing_file_is_not_set(tmp_path):
    out = load_cycle_settings(tmp_path / "nope.yaml")
    assert out == {"target_r": None, "loss_ceiling": None}


def test_load_cycle_settings_reads_owner_values(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("cycle:\n  target_r: 2.1\n  loss_ceiling: 0.01\n", encoding="utf-8")
    out = load_cycle_settings(p)
    assert math.isclose(out["target_r"], 2.1) and math.isclose(out["loss_ceiling"], 0.01)


def test_load_cycle_settings_refuses_percent_typed_as_whole_number(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("cycle:\n  loss_ceiling: 3.1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cycle_settings(p)


def test_ladder_round_trips_through_its_saved_form():
    r = _ladder(ceiling=0.05)
    r.aggressive_touch("OCL1", 100)
    r.conservative_touch("OCL1", 95)
    r.record_fill(r.orders[0], 4)
    r.invalidate("OCL1")
    doc = r.to_doc()
    back = RiskLadder.from_doc(doc)
    assert back.to_doc() == doc
    o2 = back.aggressive_touch("OCL2", 94)
    assert o2 is not None and o2.carry == r.carry


def test_refusal_by_the_loss_ceiling_is_recorded():
    r = _ladder(ceiling=0.001)  # 100 INR of planned risk at stop
    assert r.aggressive_touch("OCL1", 100) is not None  # 98 INR fits
    assert r.conservative_touch("OCL1", 95) is None  # 150 INR more: refused
    assert r.last_refused == "loss_ceiling"
    assert r.aggressive_touch("OCL1", 100) is None  # same OCL: no reason recorded as a ceiling refusal
    assert r.last_refused is None
