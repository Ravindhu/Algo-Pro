"""Parity: quant money_checks vs Blue_engine RiskGate on a fixed grid. Must show zero differences.

Blue's core/risk_gate.py uses only the standard library, so it loads here with no stubs.
The grid covers the edge cases named in DECOUPLING_PLAN stage 4: quantity 0, a missing or
bad price, a loss exactly at the limit, and exposure exactly at the cap. Breach text is
compared exactly, so any wording change shows up.
"""
import importlib.util
import itertools
import pathlib
import sys

import pytest

from quant.risk.money_checks import money_breaches

BLUE_GATE = pathlib.Path(__file__).resolve().parents[2] / "src" / "quant" / "blue_engine" / "core" / "risk_gate.py"

QTY = [-5, 0, 1, 9, 10, 11, 100, 101]
PRICE = [0.0, -1.0, float("nan"), 1.0, 99.99, 1000.0]
ACTION = ["BUY", "SELL", "HOLD"]
MAX_QTY = [None, 10]
MAX_NOTIONAL = [None, 5000.0]
MAX_LOSS = [None, 500.0]
MAX_EXPO = [None, 3000.0]
PNL = [None, 0.0, -499.0, -500.0, -501.0, 200.0]
EXPO = [None, 0.0, 2000.0, -2000.0, 3000.0]


@pytest.fixture(scope="module")
def BlueRiskGate():
    spec = importlib.util.spec_from_file_location("blue_risk_gate_ref", BLUE_GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module registered before exec
    spec.loader.exec_module(mod)
    return mod.RiskGate


def _blue_breaches(BlueRiskGate, action, qty, price, mq, mn, ml, me, pnl, expo):
    config = {k: v for k, v in {
        "max_order_quantity": mq, "max_order_notional": mn,
        "max_daily_loss": ml, "max_open_exposure": me,
    }.items() if v is not None}
    gate = BlueRiskGate(config, log=lambda *a, **k: None)
    decision = gate.check({"action": action, "quantity": qty, "price": price},
                          day_pnl=pnl, open_exposure=expo, require_intent_price=True)
    return list(decision.breaches)


def test_grid_has_no_differences_from_blue(BlueRiskGate):
    checked = 0
    breached = 0
    diffs = []
    for args in itertools.product(ACTION, QTY, PRICE, MAX_QTY, MAX_NOTIONAL, MAX_LOSS,
                                  MAX_EXPO, PNL, EXPO):
        action, qty, price, mq, mn, ml, me, pnl, expo = args
        ours = money_breaches(action, qty, price, max_quantity=mq, max_notional=mn,
                              max_daily_loss=ml, max_exposure=me, day_pnl=pnl, open_exposure=expo)
        theirs = _blue_breaches(BlueRiskGate, action, qty, price, mq, mn, ml, me, pnl, expo)
        checked += 1
        breached += bool(theirs)
        if ours != theirs:
            diffs.append((args, ours, theirs))
    assert checked > 30_000
    assert breached > 1_000, "grid must contain real breaches, or the test proves nothing"
    assert not diffs, f"{len(diffs)} differences, first: {diffs[:3]}"


@pytest.mark.parametrize("args", [
    ("BUY", 0, 100.0, 10, 5000.0, 500.0, 3000.0, 0.0, 0.0),        # zero quantity
    ("BUY", 10, 0.0, 10, 5000.0, 500.0, 3000.0, 0.0, 0.0),         # no price
    ("BUY", 10, 100.0, 10, 5000.0, 500.0, 3000.0, -500.0, 0.0),    # loss exactly at limit
    ("BUY", 10, 100.0, 10, 5000.0, 500.0, 3000.0, 0.0, 2000.0),    # 2000 + 1000 == cap, passes
    ("BUY", 10, 100.0, 10, 5000.0, 500.0, 3000.0, 0.0, 2001.0),    # just over cap
])
def test_edge_cases_match_blue(BlueRiskGate, args):
    action, qty, price, mq, mn, ml, me, pnl, expo = args
    ours = money_breaches(action, qty, price, max_quantity=mq, max_notional=mn,
                          max_daily_loss=ml, max_exposure=me, day_pnl=pnl, open_exposure=expo)
    assert ours == _blue_breaches(BlueRiskGate, action, qty, price, mq, mn, ml, me, pnl, expo)
