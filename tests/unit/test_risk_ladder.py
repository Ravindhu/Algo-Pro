"""Tests for the risk ladder (rules doc sections 11 and 14, plan P2).

The worked numbers are checked with the formula, not with the example's typed values.
"""
import math

import pytest

from quant.strategies.risk_ladder import (CONVICTION, PROBE_AGGRESSIVE, PROBE_CONSERVATIVE,
                                          RiskLadder, load_cycle_settings, shares_for, sizing_stop)

EQUITY = 100_000.0
L = 93.0
# ATR(14) used by these tests. 1.0 is below the buffer-governed stop at 100 and 95, and above it at 93.5,
# so both branches of the owner's sizing stop are exercised: max(ATR floor, (price - L) + 0.05 % of price).
A = 1.0


def _ladder(ceiling=0.05):  # wide, so mechanics tests are not cut by the owner's 1 % budget
    return RiskLadder(equity=EQUITY, htf_low=L, loss_ceiling=ceiling)


def test_sizing_stop_is_the_higher_of_atr_floor_and_buffered_stop():
    # price 100: (100 - 93) + 0.05 % x 100 = 7.05; ATR floor 1.0 is lower -> 7.05
    assert sizing_stop(100, L, 1.0) == pytest.approx(7.05)
    # price 93.5: current 0.5 + 0.04675 = 0.54675; ATR floor 1.0 is higher -> 1.0
    assert sizing_stop(93.5, L, 1.0) == pytest.approx(1.0)
    assert sizing_stop(100, L, None) is None  # no ATR: fail closed


def test_shares_formula_matches_section_14_example_with_sizing_stop():
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, 100, L, stop=sizing_stop(100, L, A)) == 14    # 100 / 7.05
    assert shares_for(EQUITY, PROBE_CONSERVATIVE, 95, L, stop=sizing_stop(95, L, A)) == 73    # 150 / 2.0475
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, 94, L, stop=sizing_stop(94, L, A)) == 95      # 100 / 1.047


def test_shares_without_stop_keep_the_plain_formula():
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, 100, L) == 14          # 100 / 7 (no sizing stop given)


def test_carry_is_added_to_fresh_shares():
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, 94, L, carry=69, stop=sizing_stop(94, L, A)) == 69 + 95


def test_price_at_or_below_htf_low_gives_zero():
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, L, L) == 0
    assert shares_for(EQUITY, PROBE_AGGRESSIVE, L - 1, L) == 0


def test_probe_then_conservative_then_invalidate_carries_unfilled():
    r = _ladder()
    o1 = r.aggressive_touch("OCL1", 100, A)
    assert o1.reason == "probe_aggressive" and o1.qty == 14
    o1c = r.conservative_touch("OCL1", 95, A)
    assert o1c.reason == "probe_conservative" and o1c.qty == 73
    r.record_fill(o1, 10)
    r.record_fill(o1c, 20)
    carried = r.invalidate("OCL1")
    assert carried == (14 - 10) + (73 - 20)  # 4 + 53 unfilled
    o2 = r.aggressive_touch("OCL2", 94, A)
    assert o2.reason == "ladder"
    assert o2.qty == carried + 95  # ladder: carried + 0.10 % at the aggressive level (stop 1.047)


def test_ladder_stops_after_third_ocl_and_only_pair_can_open():
    r = _ladder()
    r.aggressive_touch("OCL1", 100, A)
    r.invalidate("OCL1")
    r.aggressive_touch("OCL2", 97, A)
    r.invalidate("OCL2")
    o3 = r.aggressive_touch("OCL3", 96, A)
    assert o3.reason == "ladder"
    assert r.stage == "waiting_pair"
    r.invalidate("OCL3")
    assert r.aggressive_touch("OCL4", 95, A) is None  # no more probe-rate entries


def test_escalation_pair_gives_one_conviction_entry_then_waits_again():
    r = _ladder()
    for i, p in enumerate([100, 97, 96], start=1):
        r.aggressive_touch(f"OCL{i}", p, A)
        r.invalidate(f"OCL{i}")
    assert r.conservative_touch("OCL3", 95, A) is None  # arms the pair
    carry_before = r.carry
    assert carry_before > 0  # nothing filled, so every planned share is carried
    o4 = r.aggressive_touch("OCL4", 94, A)
    assert o4.reason == "conviction"
    assert o4.qty == shares_for(EQUITY, CONVICTION, 94, L, carry=carry_before, stop=sizing_stop(94, L, A))
    assert r.carry == 0  # carried shares are now inside the 0.50 % entry
    assert r.aggressive_touch("OCL5", 93.5, A) is None  # pair not armed again yet
    r.conservative_touch("OCL4", 93.5, A)
    assert r.aggressive_touch("OCL4", 93.4, A) is None  # same OCL cannot count as "new"


def test_pair_needs_new_ocl_after_conservative_touch():
    r = _ladder()
    for i, p in enumerate([100, 97, 96], start=1):
        r.aggressive_touch(f"OCL{i}", p, A)
        r.invalidate(f"OCL{i}")
    r.conservative_touch("OCL3", 95, A)
    assert r.aggressive_touch("OCL3", 94, A) is None


def test_per_cycle_loss_ceiling_refuses_over_budget_orders():
    r = _ladder(ceiling=0.001)  # 0.1 % of equity = 100 INR of planned risk at stop
    o = r.aggressive_touch("OCL1", 100, A)  # 14 sh x 7.05 = 98.7 INR: fits
    assert o is not None
    assert r.conservative_touch("OCL1", 95, A) is None  # +149.5 INR would go over
    assert r.planned_risk_inr() <= 0.001 * EQUITY


def test_owner_one_percent_ceiling_blocks_the_next_order():
    r = _ladder(ceiling=0.01)  # owner value: 1 % of 1,00,000 = 1000 INR planned risk at stop
    assert r.aggressive_touch("OCL1", 100, A) is not None       # 14 sh x 7.05 = 98.7 INR
    assert r.conservative_touch("OCL1", 95, A) is not None      # 73 sh x 2.0475 = 149.5 INR; total 248.2
    r.invalidate("OCL1")                                        # 14 + 73 = 87 unfilled shares carried
    o2 = r.aggressive_touch("OCL2", 94, A)                      # 87 carried + 95 sh (stop 1.047) = 182 sh
    assert o2 is not None and o2.qty == 182
    r.invalidate("OCL2")                                        # 182 shares carried
    o3 = r.aggressive_touch("OCL3", 93.5, A)                    # ATR floor 1.0: 182 + 100 sh = 282 sh
    assert o3 is not None and o3.qty == 282
    assert r.conservative_touch("OCL3", 93.5, A) is not None    # OCL3 completes 0.25 %: 150 sh x 1.0 = 150 INR
    assert r.planned_risk_inr() <= 0.01 * EQUITY
    assert r.planned_risk_inr() == pytest.approx(98.7 + 73 * 2.0475 + 182 * 1.047 + 282 * 1.0 + 150 * 1.0)


def test_each_ocl_totals_a_quarter_percent():
    r = _ladder(ceiling=0.05)
    o = r.aggressive_touch("OCL2_fresh", 94, A)  # 0.10 % at the aggressive level
    c = r.conservative_touch("OCL2_fresh", 93.5, A)
    assert o.risk + c.risk == 0.0025  # 0.10 % + 0.15 % = 0.25 % per LTF OCL


def test_unset_ceiling_fails_closed():
    r = RiskLadder(equity=EQUITY, htf_low=L, loss_ceiling=None)
    assert r.aggressive_touch("OCL1", 100, A) is None
    assert r.orders == []


def test_load_cycle_settings_missing_file_is_not_set(tmp_path):
    out = load_cycle_settings(tmp_path / "nope.yaml")
    assert out == {"target_r": None, "loss_ceiling": None, "portfolio_loss_ceiling": None}


def test_load_cycle_settings_reads_owner_values(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("cycle:\n  target_r: 2.1\n  loss_ceiling: 0.01\n  portfolio_loss_ceiling: 0.03\n", encoding="utf-8")
    out = load_cycle_settings(p)
    assert math.isclose(out["target_r"], 2.1) and math.isclose(out["loss_ceiling"], 0.01)
    assert math.isclose(out["portfolio_loss_ceiling"], 0.03)


def test_load_cycle_settings_refuses_percent_typed_as_whole_number(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("cycle:\n  loss_ceiling: 3.1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cycle_settings(p)


def test_ladder_round_trips_through_its_saved_form():
    r = _ladder(ceiling=0.05)
    r.aggressive_touch("OCL1", 100, A)
    r.conservative_touch("OCL1", 95, A)
    r.record_fill(r.orders[0], 4)
    r.invalidate("OCL1")
    doc = r.to_doc()
    back = RiskLadder.from_doc(doc)
    assert back.to_doc() == doc
    o2 = back.aggressive_touch("OCL2", 94, A)
    assert o2 is not None and o2.carry == r.carry


def test_refusal_by_the_loss_ceiling_is_recorded():
    r = _ladder(ceiling=0.001)  # 100 INR of planned risk at stop
    assert r.aggressive_touch("OCL1", 100, A) is not None  # 98.7 INR fits
    assert r.conservative_touch("OCL1", 95, A) is None  # 149.5 INR more: refused
    assert r.last_refused == "loss_ceiling"
    assert r.aggressive_touch("OCL1", 100, A) is None  # same OCL: no reason recorded as a ceiling refusal
    assert r.last_refused is None


def test_no_atr_means_no_order_fail_closed():
    """Owner 2026-11-20: the sizing stop needs ATR. Without it the ladder plans nothing."""
    r = _ladder()
    assert r.aggressive_touch("OCL1", 100, None) is None
    assert r.last_refused == "no_atr"
    assert r.conservative_touch("OCL1", 95, None) is None
    assert r.orders == []


def test_loss_ceiling_counts_risk_at_the_sizing_stop():
    """The 1 % ceiling must see the same stop the shares were sized on, not price - HTF L."""
    r = _ladder(ceiling=0.05)
    o = r.aggressive_touch("OCL1", 100, A)
    assert o.stop == pytest.approx(7.05)
    assert r.planned_risk_inr() == pytest.approx(o.qty * 7.05)  # not o.qty * 7.0


def test_sizing_stop_round_trips_through_the_saved_form():
    r = _ladder(ceiling=0.05)
    r.aggressive_touch("OCL1", 93.5, A)  # ATR floor branch
    back = RiskLadder.from_doc(r.to_doc())
    assert back.orders[0].stop == pytest.approx(1.0)
