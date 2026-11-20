"""Tests for the three-level risk tree (owner 2026-11-20, confirmed top-down).

Each level: 3 aggressive slots, then a wait for the conservative touch, then 2 conservative slots.
"""
import itertools
import math

import pytest

from quant.strategies.risk_tree import (AGGRESSIVE, CONSERVATIVE, LevelGate, stage_slot, tree_budgets,
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


@pytest.mark.parametrize("bad", [0.0, 0.10, 0.49, 0.81, 0.95, 1.0, -0.2, float("nan")])
def test_ratio_outside_50_to_80_is_refused_not_clamped(bad):
    with pytest.raises(ValueError):
        validate_share(bad)
    with pytest.raises(ValueError):
        tree_budgets(4.0, bad)


@pytest.mark.parametrize("good", [0.50, 0.60, 0.80])
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


# ---- stage gating -----------------------------------------------------------------------------
def test_three_aggressive_fire_then_the_level_waits():
    g = LevelGate()
    assert [g.on_aggressive_touch() for _ in range(3)] == ["aggressive"] * 3
    assert g.waiting and not g.closed
    assert g.on_aggressive_touch() is None  # no fourth aggressive slot


def test_conservative_is_refused_before_the_wait_is_over():
    g = LevelGate()
    g.on_aggressive_touch()
    assert g.on_conservative_touch() is None  # only one aggressive used: not waiting yet
    assert g.conservative_used == 0


def test_two_conservative_slots_then_the_level_is_closed():
    g = LevelGate()
    for _ in range(3):
        g.on_aggressive_touch()
    assert g.on_conservative_touch() == "conservative"
    assert g.on_conservative_touch() == "conservative"
    assert g.closed and not g.waiting
    assert g.on_conservative_touch() is None
    assert g.on_aggressive_touch() is None


def test_gate_round_trips_through_its_saved_form():
    g = LevelGate()
    for _ in range(3):
        g.on_aggressive_touch()
    g.on_conservative_touch()
    back = LevelGate.from_doc(g.to_doc())
    assert back == g
    assert back.on_conservative_touch() == "conservative"
    assert back.closed


def test_every_gate_sequence_respects_the_five_slot_limit():
    """Brute force: any order of touches never fires more than 3 aggressive or 2 conservative slots."""
    for seq in itertools.product(("a", "c"), repeat=8):
        g = LevelGate()
        for touch in seq:
            (g.on_aggressive_touch if touch == "a" else g.on_conservative_touch)()
        assert g.aggressive_used <= 3 and g.conservative_used <= 2
