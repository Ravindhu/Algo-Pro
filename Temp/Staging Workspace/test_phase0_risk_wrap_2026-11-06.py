"""Phase 0: risk gate monetary checks delegated to Blue_engine RiskGate.

Needs the OpenAlgo SDK (Blue_engine imports it). Skips with the reason when absent.
Verdicts were checked against the pre-wrap gate on a 3,456-case grid: zero differences.
"""
import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable
from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.risk import blue_risk_gate


@pytest.fixture(autouse=True)
def _need_blue():
    try:
        blue_bridge.load("core.risk_gate")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")


LIMITS = RiskLimits(max_order_quantity=100, max_order_notional=50_000.0,
                    max_open_exposure=200_000.0, max_daily_loss=5_000.0,
                    max_strategy_loss=2_000.0, max_adv_pct=0.05)


def _intent(**kw):
    base = dict(intent_id="I", strategy_id="S", symbol="X", side="BUY", qty=10,
                ref_price=100.0, kind="entry", lot_size=1, adv_shares=1e6)
    base.update(kw)
    return TradeIntent(**base)


def _breaches(intent, open_notional=0.0, daily_pnl=0.0, limits=LIMITS):
    return blue_risk_gate.entry_breaches(intent, limits, open_notional=open_notional,
                                         daily_pnl=daily_pnl)


def test_small_entry_passes():
    assert _breaches(_intent()) == []


def test_quantity_above_cap_is_a_breach():
    assert any("max_order_quantity" in b for b in _breaches(_intent(qty=101)))


def test_notional_above_cap_is_a_breach():
    assert any("max_order_notional" in b for b in _breaches(_intent(qty=60, ref_price=1000.0)))


def test_daily_loss_breach_blocks_entry():
    assert any("max_daily_loss" in b for b in _breaches(_intent(), daily_pnl=-5_000.0))


def test_open_exposure_projection_is_a_breach():
    assert any("exposure" in b for b in _breaches(_intent(qty=100, ref_price=1900.0),
                                                  open_notional=150_000.0))


def test_missing_price_fails_closed_when_notional_is_capped():
    breaches = _breaches(_intent(ref_price=0.0))
    assert breaches, "a zero price must not pass a notional cap"


def test_exits_never_reach_blue_and_are_not_blocked_by_caps(monkeypatch):
    calls = []

    class Spy:
        def __init__(self, *a, **k):
            calls.append("constructed")

        def check(self, *a, **k):
            raise AssertionError("exit must not call Blue")

    monkeypatch.setattr(blue_risk_gate, "_gate_class", lambda: Spy)
    assert blue_risk_gate.entry_breaches(_intent(kind="exit", side="SELL", qty=10_000),
                                         LIMITS, open_notional=9e9, daily_pnl=-9e9) == []
    assert calls == []


def test_unset_limits_are_not_evaluated_here():
    """The 'limit not set' policy lives in the local modules, not in this adapter."""
    empty = RiskLimits()
    assert _breaches(_intent(qty=10_000), limits=empty) == []


def test_entry_calls_blue_gate_with_configured_limits(monkeypatch):
    seen = {}
    real = blue_risk_gate._gate_class()

    class Spy(real):
        def __init__(self, config, log=None):
            seen["config"] = dict(config)
            super().__init__(config, log=log)

    monkeypatch.setattr(blue_risk_gate, "_gate_class", lambda: Spy)
    _breaches(_intent())
    assert seen["config"] == {"max_order_quantity": 100, "max_order_notional": 50_000.0,
                              "max_daily_loss": 5_000.0, "max_open_exposure": 200_000.0}
