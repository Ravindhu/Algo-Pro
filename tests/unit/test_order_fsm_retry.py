"""X-3: order state machine is retry-safe (repeated or stale reports change nothing).

Flow stage: Order state machine
Status: TEST
"""
import pytest

from quant.execution.order_fsm import InvalidTransition, OrderFSM, OrderState


def submitted(qty=10):
    f = OrderFSM("I1", qty)
    f.move(OrderState.SUBMITTED)
    return f


def test_repeated_accept_is_noop():
    f = submitted()
    f.move(OrderState.ACCEPTED)
    f.move(OrderState.ACCEPTED)
    assert f.state is OrderState.ACCEPTED and f.filled_qty == 0


def test_repeated_submit_is_noop():
    f = submitted()
    f.move(OrderState.SUBMITTED)
    assert f.state is OrderState.SUBMITTED


def test_repeated_fill_with_same_qty_is_noop():
    f = submitted()
    f.move(OrderState.FILLED, 10)
    f.move(OrderState.FILLED, 10)
    f.move(OrderState.FILLED)
    assert f.state is OrderState.FILLED and f.filled_qty == 10


def test_conflicting_repeat_fill_is_refused():
    f = submitted()
    f.move(OrderState.FILLED, 10)
    with pytest.raises(ValueError):
        f.move(OrderState.FILLED, 7)
    assert f.filled_qty == 10


def test_repeated_partial_with_same_qty_is_noop():
    f = submitted()
    f.move(OrderState.PARTIAL, 4)
    f.move(OrderState.PARTIAL, 4)
    assert f.state is OrderState.PARTIAL and f.filled_qty == 4


def test_stale_partial_cannot_lower_fill():
    f = submitted()
    f.move(OrderState.PARTIAL, 6)
    with pytest.raises(ValueError):
        f.move(OrderState.PARTIAL, 4)
    assert f.filled_qty == 6


def test_failed_move_leaves_state_unchanged():
    f = submitted()
    with pytest.raises(ValueError):
        f.move(OrderState.FILLED, 5)
    assert f.state is OrderState.SUBMITTED and f.filled_qty == 0


def test_terminal_state_cannot_change_to_other_state():
    f = submitted()
    f.move(OrderState.FILLED, 10)
    with pytest.raises(InvalidTransition):
        f.move(OrderState.CANCELLED)
    with pytest.raises(InvalidTransition):
        f.move(OrderState.REJECTED)
    assert f.state is OrderState.FILLED
