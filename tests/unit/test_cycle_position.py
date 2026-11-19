"""Cycle position rules (rules doc sections 11 and 12): one quantity, POV fills, pause and resume."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

from quant.execution.cycle_position import CyclePosition, size_cycle  # noqa: E402


def test_size_once_from_risk_and_htf_stop():
    # risk 5000, trigger 11.0, HTF low 9.0 -> per share 2.0 -> 2500 shares
    assert size_cycle(5000.0, 11.0, 9.0) == 2500


def test_bad_inputs_give_zero_quantity():
    assert size_cycle(5000.0, 9.0, 9.0) == 0
    assert size_cycle(0.0, 11.0, 9.0) == 0


def test_later_triggers_keep_the_same_quantity():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(1000, 11.0)
    pos.on_trigger(conservative=True)
    assert pos.max_qty == 2500 and pos.remaining == 1500  # no extra quantity added


def test_ltf_invalidation_pauses_and_next_ocl_resumes_with_remaining():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(1000, 11.0)
    pos.on_ltf_invalidated()
    assert pos.status == "paused"
    pos.record_fill(500, 10.0)  # POV is stopped while paused: no fill
    assert pos.filled == 1000
    pos.on_trigger()            # next LTF OCL touch resumes with the remaining quantity
    assert pos.status == "filling" and pos.remaining == 1500
    pos.record_fill(500, 10.0)
    assert pos.filled == 1500 and pos.remaining == 1000


def test_average_is_quantity_weighted_fill_price():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(1000, 11.0)
    pos.record_fill(1000, 9.0)
    assert pos.avg_price == pytest.approx((1000 * 11.0 + 1000 * 9.0) / 2000)


def test_fill_cannot_exceed_the_cycle_quantity():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(99999, 11.0)
    assert pos.filled == pos.max_qty and pos.status == "complete"


def test_htf_invalidation_closes_and_drops_remaining():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(500, 11.0)
    pos.on_htf_invalidated()
    assert pos.status == "closed"
    pos.on_trigger()
    assert pos.status == "closed"


def test_exit_mode_trail_after_conservative_touch():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(1000, 11.0)
    pos.on_trigger(conservative=True)
    assert pos.exit_mode() == ("trail", None)


def test_exit_mode_target_uses_average_and_r():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.target_r = 3.0
    pos.on_trigger()
    pos.record_fill(1000, 11.0)
    mode, target = pos.exit_mode()
    assert mode == "target" and target == pytest.approx(11.0 + 3.0 * 2.0)


def test_exit_mode_pending_until_backtest_sets_n():
    pos = CyclePosition.open_at("c1", htf_low=9.0, risk_amount=5000.0, trigger_price=11.0)
    pos.on_trigger()
    pos.record_fill(1000, 11.0)
    assert pos.exit_mode() == ("target_pending", None)
