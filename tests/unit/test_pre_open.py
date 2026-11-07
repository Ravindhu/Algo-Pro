"""NSE cash pre-open phases (flow item 9), per NSE/CMTR/41180. Times are passed in; no clock."""
from datetime import time

from quant.core import pre_open as po


def test_phase_boundaries():
    assert po.pre_open_phase(time(8, 59, 59)) == po.CLOSED
    assert po.pre_open_phase(time(9, 0)) == po.ENTRY_OPEN
    assert po.pre_open_phase(time(9, 6, 59)) == po.ENTRY_OPEN
    assert po.pre_open_phase(time(9, 7)) == po.ENTRY_UNCERTAIN
    assert po.pre_open_phase(time(9, 7, 59)) == po.ENTRY_UNCERTAIN
    assert po.pre_open_phase(time(9, 8)) == po.MATCHING
    assert po.pre_open_phase(time(9, 12)) == po.BUFFER
    assert po.pre_open_phase(time(9, 15)) == po.OPEN


def test_entries_refused_in_uncertain_window():
    assert po.order_allowed(po.ENTRY_UNCERTAIN, "entry", "LIMIT") is False


def test_market_orders_refused_in_pre_open_but_fine_in_normal_session():
    assert po.order_allowed(po.ENTRY_OPEN, "entry", "MARKET") is False
    assert po.order_allowed(po.OPEN, "entry", "MARKET") is True


def test_limit_entry_allowed_before_0907_only():
    assert po.order_allowed(po.ENTRY_OPEN, "entry", "LIMIT") is True
    for phase in (po.CLOSED, po.ENTRY_UNCERTAIN, po.MATCHING, po.BUFFER):
        assert po.order_allowed(phase, "entry", "LIMIT") is False
