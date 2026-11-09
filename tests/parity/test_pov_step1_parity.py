"""Parity (decouple 6.5, Step 1): local POV throttle and TCA vs the real Blue functions.

Explicit request_data and explicit clocks are passed, so Blue's import-time env reads do not matter.
"""
from datetime import datetime, timedelta

import numpy as np
import pytest

from quant.execution import pov_tca
from quant.execution.pov_throttle import PovThrottleState, should_throttle_order

BASE = datetime(2026, 11, 9, 10, 0, 0)


@pytest.mark.parametrize("seed", range(120))
def test_throttle_matches_blue(blue, seed):
    th_b = blue("core.throttle")
    rng = np.random.default_rng(seed)
    req = {
        "cancellation_cool_off_seconds": float(rng.choice([0.0, 1.0, 2.0, 5.0])),
        "min_order_interval_seconds": float(rng.choice([0.0, 0.5, 1.0, 3.0])),
        "max_order_to_trade_ratio": float(rng.choice([1.0, 3.0, 10.0])),
        "interval": float(rng.choice([0.05, 1.0, 2.5])),
    }
    now = BASE
    state = PovThrottleState(order_count=int(rng.integers(0, 12)),
                             filled_quantity=int(rng.integers(0, 40)),
                             exit_filled_quantity=int(rng.integers(0, 10)))
    if rng.random() < 0.7:
        state.last_order_at = BASE - timedelta(seconds=float(rng.uniform(0, 4)))
    if rng.random() < 0.5:
        state.last_cancel_at = BASE - timedelta(seconds=float(rng.uniform(0, 6)))

    class Snap:  # Blue reads these attributes by name
        order_count = state.order_count
        filled_quantity = state.filled_quantity
        exit_filled_quantity = state.exit_filled_quantity
        last_order_at = state.last_order_at
        last_cancel_at = state.last_cancel_at

    want = th_b.should_throttle_order(execution=Snap, request_data=req, now=now)
    got = should_throttle_order(state, req, now=now)
    assert got[0] == want[0]
    assert got[1] == pytest.approx(want[1], abs=1e-12)
    assert got[2] == want[2]


@pytest.mark.parametrize("phase", ["accumulating", "exiting"])
@pytest.mark.parametrize("seed", range(40))
def test_tca_matches_blue(blue, seed, phase):
    eng = blue("pov.pov_engine").PovEngine
    rng = np.random.default_rng(seed)
    n = int(rng.integers(0, 6))
    children = []
    for _ in range(n):
        children.append({
            "attempted": int(rng.integers(0, 200)),
            "ltp_at_place": (None if rng.random() < 0.1 else float(rng.uniform(50, 150))),
        })
    summary = {"filled": int(rng.integers(0, 500)), "target": int(rng.integers(0, 500)),
               "children": children, "phase": phase}
    if rng.random() < 0.2:
        summary["child_orders"] = int(rng.integers(0, 9))
    assert pov_tca.execution_cost(summary) == eng._execution_cost(summary)
