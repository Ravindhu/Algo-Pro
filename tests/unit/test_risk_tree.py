"""Tests for the three-level risk tree (owner 2026-11-20, confirmed top-down).

Each level: 3 aggressive slots, then a wait for the conservative touch, then 2 conservative slots.
"""
import itertools
import math

import pytest

from quant.engines.alpha.strategies.risk_tree import (AGGRESSIVE, CONSERVATIVE, SlotGate, plan, stage_slot, tree_budgets,
                                        validate_share)

STAGES = (AGGRESSIVE, CONSERVATIVE)


def test_owner_numbers_at_the_4_percent_reference_and_default_60_40():
    t = tree_budgets(4.0)  # default 60/40
    assert t.htf_slot[AGGRESSIVE] == pytest.approx(0.008)     # 0.8 % per HTF OCL
    assert t.htf_slot[CONSERVATIVE] == pytest.approx(0.008)
    assert t.ltf_slot[(AGGRESSIVE, AGGRESSIVE)] == pytest.approx(0.0016)  # 0.16 % per LTF OCL
    assert t.entry[(AGGRESSIVE, AGGRESSIVE, AGGRESSIVE)] == pytest.approx(0.00032)  # 0.032 % per entry


def test_one_percent_allocation_is_a_quarter_of_the_reference():
    ref = tree_budgets(4.0)
    one = tree_budgets(1.0)
    for st in STAGES:
        assert one.htf_slot[st] == pytest.approx(ref.htf_slot[st] / 4)
    for key in ref.entry:
        assert one.entry[key] == pytest.approx(ref.entry[key] / 4)


@pytest.mark.parametrize("share", [0.50, 0.55, 0.60, 0.70, 0.80])
def test_every_level_splits_exactly_into_its_five_slots(share):
    """3 aggressive + 2 conservative must add back to the parent, at every level and every ratio."""
    t = tree_budgets(4.0, share)
    total_htf = 3 * t.htf_slot[AGGRESSIVE] + 2 * t.htf_slot[CONSERVATIVE]
    assert total_htf == pytest.approx(t.allocation)
    for h in STAGES:
        total_ltf = 3 * t.ltf_slot[(h, AGGRESSIVE)] + 2 * t.ltf_slot[(h, CONSERVATIVE)]
        assert total_ltf == pytest.approx(t.htf_slot[h])
        for l in STAGES:
            total_entry = 3 * t.entry[(h, l, AGGRESSIVE)] + 2 * t.entry[(h, l, CONSERVATIVE)]
            assert total_entry == pytest.approx(t.ltf_slot[(h, l)])


@pytest.mark.parametrize("bad", [0.0, 0.10, 0.29, 0.81, 0.95, 1.0, -0.2, float("nan")])
def test_ratio_outside_30_to_80_is_refused_not_clamped(bad):
    with pytest.raises(ValueError):
        validate_share(bad)
    with pytest.raises(ValueError):
        tree_budgets(4.0, bad)


@pytest.mark.parametrize("good", [0.30, 0.50, 0.60, 0.80])
def test_ratio_bounds_are_inclusive(good):
    assert validate_share(good) == good


def test_non_positive_allocation_is_refused():
    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            tree_budgets(bad)


def test_stage_slot_uses_the_share_not_a_fixed_split():
    assert stage_slot(0.04, AGGRESSIVE, 0.8) == pytest.approx(0.04 * 0.8 / 3)
    assert stage_slot(0.04, CONSERVATIVE, 0.8) == pytest.approx(0.04 * 0.2 / 2)
    with pytest.raises(ValueError):
        stage_slot(0.04, "middle", 0.6)


# ---- slot gate: arming ------------------------------------------------------------------------
def test_three_aggressive_slots_then_wait():
    g = SlotGate()
    assert [g.on_new_ocl() for _ in range(3)] == [AGGRESSIVE] * 3
    assert g.waiting and not g.closed
    assert g.on_new_ocl() is None  # no fourth aggressive slot while waiting


def test_conservative_touch_arms_and_the_next_new_ocl_takes_the_slot():
    g = SlotGate()
    for _ in range(3):
        g.on_new_ocl()
    assert g.on_conservative_touch() is True
    assert g.on_new_ocl() == CONSERVATIVE
    assert g.armed is False


def test_a_new_ocl_without_a_conservative_touch_is_refused():
    g = SlotGate()
    for _ in range(3):
        g.on_new_ocl()
    assert g.on_new_ocl() is None  # waiting, not armed


def test_conservative_touch_before_the_wait_is_ignored():
    g = SlotGate()
    g.on_new_ocl()
    assert g.on_conservative_touch() is False
    assert g.armed is False


def test_two_conservative_slots_then_closed():
    g = SlotGate()
    for _ in range(3):
        g.on_new_ocl()
    for _ in range(2):
        g.on_conservative_touch()
        assert g.on_new_ocl() == CONSERVATIVE
    assert g.closed
    g.on_conservative_touch()
    assert g.on_new_ocl() is None


def test_slot_gate_round_trips_through_its_saved_form():
    g = SlotGate()
    for _ in range(3):
        g.on_new_ocl()
    g.on_conservative_touch()
    back = SlotGate.from_doc(g.to_doc())
    assert back == g
    assert back.on_new_ocl() == CONSERVATIVE


def test_every_touch_order_respects_the_slot_limits():
    """Brute force over all 10-event orders of new-OCL and conservative-touch events."""
    for seq in itertools.product(("n", "t"), repeat=10):
        g = SlotGate()
        for ev in seq:
            g.on_new_ocl() if ev == "n" else g.on_conservative_touch()
        assert g.aggressive_used <= 3 and g.conservative_used <= 2


# ---- planner ------------------------------------------------------------------------------------
def _budgets_of(decisions, level=None):
    return [d for d in decisions if (level is None or d.level == level) and d.refused is None and d.fraction > 0]


def test_full_cycle_uses_all_five_htf_slots_and_five_ltf_slots_each():
    """4 % allocation, 60/40: 5 HTF OCL slots of 0.8 %; each HTF OCL has 5 LTF OCL slots of 0.16 %."""
    events = []
    for h in range(5):
        if h >= 3:
            events.append(("mtf_cons_touch",))  # each conservative HTF slot needs its own touch
        events.append(("htf_ocl", f"H{h}"))
        for l in range(5):
            if l >= 3:
                events.append(("htf_cons_touch",))  # and each conservative LTF slot its own touch
            events.append(("ltf_ocl", f"H{h}L{l}"))
            events.append(("ltf_invalidated", f"H{h}L{l}"))
        events.append(("htf_invalidated",))
    d = plan(events, 4.0)
    htf_slots = [x for x in d if x.event[0] == "htf_ocl" and x.refused is None]
    ltf_slots = [x for x in d if x.event[0] == "ltf_ocl" and x.refused is None]
    assert len(htf_slots) == 5
    assert len(ltf_slots) == 25
    assert all(x.fraction == pytest.approx(0.008) for x in htf_slots)
    assert all(x.fraction == pytest.approx(0.0016) for x in ltf_slots)
    assert sum(x.fraction for x in htf_slots) == pytest.approx(0.04)


def test_leaf_splits_60_40_between_its_own_aggressive_and_conservative_touch():
    events = [("htf_ocl", "H1"), ("ltf_ocl", "L1"), ("ltf_agg_touch", "L1"), ("ltf_cons_touch", "L1")]
    d = plan(events, 4.0)
    agg = [x for x in d if x.event[0] == "ltf_agg_touch"][0]
    cons = [x for x in d if x.event[0] == "ltf_cons_touch"][0]
    assert agg.fraction == pytest.approx(0.0016 * 0.6)  # 0.096 %
    assert cons.fraction == pytest.approx(0.0016 * 0.4)  # 0.064 %
    assert agg.fraction + cons.fraction == pytest.approx(0.0016)


def test_each_leaf_touch_is_used_once_and_only_while_the_ocl_is_open():
    events = [("htf_ocl", "H1"), ("ltf_ocl", "L1"), ("ltf_agg_touch", "L1"), ("ltf_agg_touch", "L1"),
              ("ltf_invalidated", "L1"), ("ltf_cons_touch", "L1")]
    d = plan(events, 4.0)
    assert d[2].refused is None
    assert d[3].refused == "already_used"
    assert d[5].refused == "no_open_ltf_ocl"


def test_ltf_slot_refused_when_htf_is_waiting_for_its_conservative_touch():
    events = [("htf_ocl", "H1"), ("ltf_ocl", "a"), ("ltf_invalidated", "a"), ("ltf_ocl", "b"),
              ("ltf_invalidated", "b"), ("ltf_ocl", "c"), ("ltf_invalidated", "c"), ("ltf_ocl", "d")]
    d = plan(events, 4.0)
    assert d[-1].refused == "htf_slots_used_or_not_armed"


def test_htf_conservative_slot_is_sized_at_the_conservative_stage_budget():
    """Under 80/20 the conservative LTF slot is smaller than the aggressive one."""
    events = [("htf_ocl", "H1"), ("ltf_ocl", "a"), ("ltf_invalidated", "a"),
              ("ltf_ocl", "b"), ("ltf_invalidated", "b"), ("ltf_ocl", "c"), ("ltf_invalidated", "c"),
              ("htf_cons_touch",), ("ltf_ocl", "d")]
    d = plan(events, 4.0, 0.8)
    agg_slot = d[1].fraction
    cons_slot = d[-1].fraction
    # H1 is an MTF aggressive OCL: budget 4 % x 0.8/3. Its LTF slots: aggressive 0.8/3, conservative 0.2/2.
    htf_budget = 0.04 * 0.8 / 3
    assert agg_slot == pytest.approx(htf_budget * 0.8 / 3)
    assert cons_slot == pytest.approx(htf_budget * 0.2 / 2)
    assert cons_slot < agg_slot


def test_mtf_conservative_slot_is_refused_until_the_touch_arms_it():
    events = [("htf_ocl", "H1"), ("htf_invalidated",), ("htf_ocl", "H2"), ("htf_invalidated",),
              ("htf_ocl", "H3"), ("htf_invalidated",), ("htf_ocl", "H4")]
    d = plan(events, 4.0)
    assert d[-1].refused == "mtf_slots_used_or_not_armed"
    events2 = events[:-1] + [("mtf_cons_touch",), ("htf_ocl", "H4")]
    d2 = plan(events2, 4.0)
    assert d2[-1].stage == CONSERVATIVE and d2[-1].fraction == pytest.approx(0.008)


def test_plan_is_stateless_for_the_same_input():
    events = [("htf_ocl", "H1"), ("ltf_ocl", "a"), ("ltf_agg_touch", "a"), ("ltf_cons_touch", "a")]
    assert plan(events, 4.0) == plan(events, 4.0)


def test_one_percent_allocation_scales_every_decision_by_a_quarter():
    events = [("htf_ocl", "H1"), ("ltf_ocl", "a"), ("ltf_agg_touch", "a"), ("ltf_cons_touch", "a")]
    four = plan(events, 4.0)
    one = plan(events, 1.0)
    for x, y in zip(four, one):
        assert y.fraction == pytest.approx(x.fraction / 4)
