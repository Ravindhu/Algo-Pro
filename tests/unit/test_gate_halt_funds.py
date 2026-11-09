"""Gate checks for circuit freeze (item 7) and funds (item 10). Entries only; exits pass."""
import pytest

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler, index_move_freezes
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch

FULL = RiskLimits(
    max_order_quantity=1000, max_order_notional=5e6, max_symbols_per_portfolio=10, max_open_exposure=2e7,
    max_daily_loss=1e5, max_strategy_loss=5e4, max_adv_pct=0.05,
)


def entry(**kw):
    base = dict(intent_id="I1", strategy_id="S1", symbol="RELIANCE", side="BUY", qty=10,
                ref_price=1000.0, kind="entry", lot_size=1, prev_close=990.0,
                circuit_pct=0.2, adv_shares=1e6, asm_gsm=False)
    base.update(kw)
    return TradeIntent(**base)


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(tmp_path / "kill.json")


def test_baseline_entry_is_approved(kill):
    d = check_intent(entry(), RiskState(), FULL, kill, SafeMode())
    assert d.approved, d.reasons


def test_frozen_index_refuses_entries(kill):
    halt = HaltHandler()
    halt.freeze("NIFTY")
    d = check_intent(entry(), RiskState(), FULL, kill, SafeMode(), halt=halt)
    assert not d.approved
    assert any("circuit freeze" in r for r in d.reasons)


def test_frozen_index_does_not_block_exits(kill):
    halt = HaltHandler()
    halt.freeze("NIFTY")
    d = check_intent(entry(kind="exit", intent_id="X1", side="SELL"), RiskState(), FULL, kill, SafeMode(), halt=halt)
    assert not any("circuit freeze" in r for r in d.reasons)


def test_insufficient_funds_refuses_entry(kill):
    # notional = 10 * 1000 = 10,000
    d = check_intent(entry(), RiskState(), FULL, kill, SafeMode(), available_funds=9_999.0)
    assert not d.approved
    assert any("insufficient funds" in r for r in d.reasons)


def test_enough_funds_passes(kill):
    d = check_intent(entry(), RiskState(), FULL, kill, SafeMode(), available_funds=10_000.0)
    assert d.approved, d.reasons


def test_funds_none_means_not_checked(kill):
    d = check_intent(entry(), RiskState(), FULL, kill, SafeMode(), available_funds=None)
    assert d.approved, d.reasons


def test_index_move_freezes_at_ten_percent():
    halt = HaltHandler()
    assert index_move_freezes(halt, "NIFTY", prev_close=100.0, last=90.0) is True
    assert halt.is_frozen("NIFTY")


def test_small_index_move_does_not_freeze():
    halt = HaltHandler()
    assert index_move_freezes(halt, "NIFTY", prev_close=100.0, last=95.0) is False
    assert not halt.is_frozen("NIFTY")
