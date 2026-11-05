"""X-8: event blackout blocks new entries at the Risk gate, never exits.

Flow stage: Event blackout, Risk gate
Status: TEST

The Blue_engine monetary check (entry_breaches) needs OpenAlgo, so it is stubbed
here. Blackout behaviour does not depend on it.
"""
from datetime import date

import pytest

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.data.market_inputs import event_blackout_dates
from quant.monitor.safe_mode import SafeMode
from quant.risk import gate as gate_mod
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch

FULL = RiskLimits(
    max_order_quantity=1000, max_order_notional=5e6, max_open_exposure=2e7,
    max_daily_loss=1e5, max_strategy_loss=5e4, max_adv_pct=0.05,
)
RESULTS_DAY = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def no_blue_monetary(monkeypatch):
    monkeypatch.setattr(gate_mod, "entry_breaches", lambda *a, **k: [])


@pytest.fixture
def kill(tmp_path):
    return KillSwitch(tmp_path / "kill.json")


def intent(**kw):
    base = dict(intent_id="I1", strategy_id="S1", symbol="ABFRL", side="BUY", qty=10,
                ref_price=1000.0, kind="entry", lot_size=1, prev_close=990.0,
                circuit_pct=0.2, adv_shares=1e6, asm_gsm=False, trade_day=RESULTS_DAY)
    base.update(kw)
    return TradeIntent(**base)


def blackout_state(symbol="ABFRL", day=RESULTS_DAY):
    return RiskState(blackout_dates={symbol: {day}})


def test_entry_on_blackout_day_is_blocked(kill):
    d = check_intent(intent(), blackout_state(), FULL, kill, SafeMode())
    assert not d.approved
    assert any("event blackout on 2026-11-10" in r for r in d.reasons)


def test_exit_on_blackout_day_is_allowed(kill):
    d = check_intent(intent(kind="exit", side="SELL"), blackout_state(), FULL, kill, SafeMode())
    assert d.approved and d.reasons == ()


def test_protection_order_on_blackout_day_is_allowed(kill):
    d = check_intent(intent(kind="protection", side="SELL"), blackout_state(), FULL, kill, SafeMode())
    assert d.approved


def test_entry_day_before_and_after_window_is_allowed(kill):
    before = intent(trade_day=date(2026, 11, 9))
    after = intent(trade_day=date(2026, 11, 11))
    assert check_intent(before, blackout_state(), FULL, kill, SafeMode()).approved
    assert check_intent(after, blackout_state(), FULL, kill, SafeMode()).approved


def test_blackout_is_per_symbol(kill):
    d = check_intent(intent(symbol="RELIANCE"), blackout_state("ABFRL"), FULL, kill, SafeMode())
    assert d.approved


def test_missing_trade_day_fails_closed_when_blackout_exists(kill):
    d = check_intent(intent(trade_day=None), blackout_state(), FULL, kill, SafeMode())
    assert not d.approved
    assert any("trade_day missing" in r for r in d.reasons)


def test_missing_trade_day_passes_when_no_blackout_for_symbol(kill):
    d = check_intent(intent(trade_day=None), RiskState(), FULL, kill, SafeMode())
    assert d.approved


def test_blackout_from_nse_events_blocks_window(kill):
    # Board-meeting event on RESULTS_DAY, default window: 1 day before and 1 day after.
    events = [{"symbol": "ABFRL", "date": RESULTS_DAY}]
    state = RiskState(blackout_dates={"ABFRL": event_blackout_dates(events, "ABFRL")})
    assert not check_intent(intent(trade_day=date(2026, 11, 9)), state, FULL, kill, SafeMode()).approved
    assert not check_intent(intent(trade_day=date(2026, 11, 11)), state, FULL, kill, SafeMode()).approved
    assert check_intent(intent(trade_day=date(2026, 11, 12)), state, FULL, kill, SafeMode()).approved


def test_blackout_does_not_hide_other_block_reasons(kill):
    # Missing quantity limit still blocks, and the blackout reason is reported too.
    d = check_intent(intent(), blackout_state(), RiskLimits(), kill, SafeMode())
    assert not d.approved
    assert any("event blackout" in r for r in d.reasons)
    assert any("limit not set" in r for r in d.reasons)
