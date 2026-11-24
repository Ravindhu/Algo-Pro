"""Flow stage: Signal and conviction (ROC confirmation and state machine).
Status: TESTS WRITTEN; run result is in the commit message.
"""
import pytest

from quant.indicators.roc import find_flip, rate_of_change, zscored_roc
from quant.strategies.signals.confirm_state import (
    STATE_ARMED,
    STATE_CONFIRMED,
    STATE_IDLE,
    ConfirmMachine,
)


def test_roc_first_bars_none_then_value():
    out = rate_of_change([10.0, 11.0, 12.0], period=1)
    assert out[0] is None
    assert out[1] == pytest.approx(10.0)


def test_roc_zero_prev_is_none():
    out = rate_of_change([0.0, 5.0], period=1)
    assert out[1] is None


def test_roc_bad_period_raises():
    with pytest.raises(ValueError):
        rate_of_change([1.0, 2.0], period=0)


def test_zscore_needs_full_window():
    closes = [float(i) for i in range(1, 10)]
    z = zscored_roc(closes, period=1, z_window=5)
    assert all(v is None for v in z[:5])


def test_zscore_flat_series_is_none():
    z = zscored_roc([10.0] * 30, period=1, z_window=5)
    assert all(v is None for v in z)


def test_find_flip_crossing_only():
    z = [0.0, 0.5, 1.2, 1.5, 0.9]
    assert find_flip(z, 0, entry_z=1.0, direction="BUY") == 2


def test_find_flip_sell_direction():
    z = [0.0, -0.5, -1.2, -1.5]
    assert find_flip(z, 0, entry_z=1.0, direction="SELL") == 2


def test_find_flip_bad_direction_raises():
    with pytest.raises(ValueError):
        find_flip([0.0, 2.0], 0, direction="HOLD")


def test_machine_starts_idle_and_arm_then_confirm():
    m = ConfirmMachine()
    assert m.state == STATE_IDLE
    assert m.arm(level=100.0, action="buy", bar_idx=3) is True
    assert m.state == STATE_ARMED
    assert m.s.action == "BUY"
    assert m.confirm(z_value=1.4, bar_idx=6) is True
    assert m.state == STATE_CONFIRMED
    assert m.s.bars_waited == 3


def test_second_touch_does_not_rearm():
    m = ConfirmMachine()
    m.arm(level=100.0, action="BUY", bar_idx=1)
    assert m.arm(level=99.0, action="BUY", bar_idx=2) is False
    assert m.s.level == 100.0


def test_confirm_without_arm_is_rejected():
    m = ConfirmMachine()
    assert m.confirm(z_value=2.0, bar_idx=1) is False
    assert m.state == STATE_IDLE


def test_reset_allows_new_arm():
    m = ConfirmMachine()
    m.arm(level=100.0, action="BUY", bar_idx=1)
    m.confirm(z_value=2.0, bar_idx=2)
    m.reset()
    assert m.state == STATE_IDLE
    assert m.arm(level=101.0, action="BUY", bar_idx=5) is True


def test_snapshot_is_plain_dict():
    m = ConfirmMachine()
    m.arm(level=100.0, action="BUY", bar_idx=1, meta={"k": 1})
    snap = m.snapshot()
    assert snap["state"] == STATE_ARMED
    assert snap["meta"] == {"k": 1}
