"""Halt-all blocks everything except kill closes, and only while the kill switch is on (decision 0020)."""
import pytest

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch

FULL = RiskLimits(
    max_order_quantity=1000, max_order_notional=5e6, max_symbols_per_portfolio=10, max_open_exposure=2e7,
    max_daily_loss=1e5, max_strategy_loss=5e4, max_adv_pct=0.05,
)


def exit_intent(strategy_id="S1", intent_id="X1"):
    return TradeIntent(intent_id=intent_id, strategy_id=strategy_id, symbol="A", side="SELL", qty=5,
                       ref_price=100.0, kind="exit")


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(tmp_path / "kill.json")


@pytest.fixture
def halt():
    s = SafeMode()
    s.set(Mode.HALT_ALL, "test")
    return s


def test_halt_all_blocks_a_normal_exit(kill, halt):
    d = check_intent(exit_intent(), RiskState(), FULL, kill, halt)
    assert not d.approved and "halt-all" in " ".join(d.reasons)


def test_halt_all_lets_a_kill_close_through_when_switch_is_on(kill, halt):
    kill.trip("test")
    d = check_intent(exit_intent(strategy_id="KILL", intent_id="KILL-A-1"), RiskState(), FULL, kill, halt)
    assert d.approved, d.reasons


def test_halt_all_does_not_pass_a_fake_kill_close_when_switch_is_off(kill, halt):
    d = check_intent(exit_intent(strategy_id="KILL", intent_id="KILL-A-1"), RiskState(), FULL, kill, halt)
    assert not d.approved and "halt-all" in " ".join(d.reasons)


def test_kill_close_still_blocked_by_risk_limits_blocked(kill, halt):
    kill.trip("test")
    limits = RiskLimits(**{**FULL.__dict__, "blocked": True})
    d = check_intent(exit_intent(strategy_id="KILL", intent_id="KILL-A-1"), RiskState(), limits, kill, halt)
    assert not d.approved and "blocked" in " ".join(d.reasons)
