"""Hand-checked tests for POV Step 1: throttle state and rules, TCA. Worked out by hand."""
from datetime import datetime, timedelta

from quant.execution.pov_tca import execution_cost
from quant.execution.pov_throttle import PovThrottleState, should_throttle_order

T0 = datetime(2026, 11, 9, 10, 0, 0)


def test_order_interval_wait_is_the_remaining_gap():
    st = PovThrottleState(last_order_at=T0)
    # Min gap 1.0 s; 0.4 s elapsed -> wait 0.6 s.
    throttled, delay, reason = should_throttle_order(st, {"min_order_interval_seconds": 1.0},
                                                     now=T0 + timedelta(seconds=0.4))
    assert throttled and reason == "order interval"
    assert abs(delay - 0.6) < 1e-9


def test_cancel_cool_off_wins_when_longer_than_interval():
    st = PovThrottleState(last_order_at=T0, last_cancel_at=T0)
    # Cool-off 2 s and interval 1 s, nothing elapsed: the longer wait (2 s) is reported.
    throttled, delay, reason = should_throttle_order(
        st, {"cancellation_cool_off_seconds": 2.0, "min_order_interval_seconds": 1.0}, now=T0)
    assert throttled and reason == "cancellation cooling-off" and delay == 2.0


def test_no_wait_after_the_gap():
    st = PovThrottleState(last_order_at=T0)
    assert should_throttle_order(st, {"min_order_interval_seconds": 1.0},
                                 now=T0 + timedelta(seconds=1.0)) == (False, 0.0, None)


def test_order_to_trade_ratio_blocks_when_orders_reach_limit():
    # 4 fills, ratio 2 -> limit ceil(4*2)=8. 8 orders hits the limit.
    st = PovThrottleState(order_count=8, filled_quantity=4)
    throttled, delay, reason = should_throttle_order(st, {"max_order_to_trade_ratio": 2.0,
                                                          "interval": 0.05}, now=T0)
    assert throttled and reason == "order-to-trade ratio"
    assert delay == 0.1  # max(0.1, 0.05)


def test_exit_fills_count_toward_the_ratio():
    st = PovThrottleState(order_count=4, filled_quantity=1, exit_filled_quantity=1)
    # ratio 2 -> limit ceil(2*2)=4; 4 orders >= 4 -> throttled.
    assert should_throttle_order(st, {"max_order_to_trade_ratio": 2.0}, now=T0)[0] is True


def test_record_helpers_update_state():
    st = PovThrottleState()
    st.record_order(T0)
    st.record_cancel(T0 + timedelta(seconds=1))
    st.record_fill(5)
    st.record_fill(3, exit_leg=True)
    st.record_fill(0)
    assert (st.order_count, st.last_order_at, st.last_cancel_at) == (1, T0, T0 + timedelta(seconds=1))
    assert (st.filled_quantity, st.exit_filled_quantity) == (5, 3)


def test_two_runs_do_not_share_throttle_state():
    a, b = PovThrottleState(), PovThrottleState()
    a.record_order(T0)
    assert b.order_count == 0 and b.last_order_at is None  # no module-level leak


def test_tca_hand_checked_drift():
    # First LTP 100, last LTP 101 -> drift +100 bps. Accumulating: adverse = +100.
    summary = {"filled": 50, "target": 100, "phase": "accumulating",
               "children": [{"attempted": 20, "ltp_at_place": 100.0},
                            {"attempted": 30, "ltp_at_place": 101.0}]}
    m = execution_cost(summary)
    assert m["fill_rate"] == 0.5
    assert m["child_orders"] == 2
    assert m["avg_child_qty"] == 25.0
    assert m["price_drift_bps"] == 100.0
    assert m["adverse_drift_bps"] == 100.0


def test_tca_exiting_flips_adverse_sign():
    summary = {"filled": 10, "target": 10, "phase": "exiting",
               "children": [{"attempted": 5, "ltp_at_place": 100.0},
                            {"attempted": 5, "ltp_at_place": 101.0}]}
    assert execution_cost(summary)["adverse_drift_bps"] == -100.0


def test_tca_missing_fields_give_none_not_error():
    m = execution_cost({"filled": 0, "target": 0, "children": [{"attempted": 3, "ltp_at_place": "bad"}]})
    assert m["fill_rate"] is None and m["price_drift_bps"] is None and m["avg_child_qty"] == 3.0
