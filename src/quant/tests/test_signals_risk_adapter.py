"""Flow stage: Trade intent (signal -> live contract -> Risk gate).
Status: TESTS WRITTEN. Runs the real quant.risk.gate.check_intent; nothing is mocked.
"""
import pytest

from quant.core.config import RiskLimits
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch
from quant.strategies.signals.risk_adapter import (
    gate_signal,
    levels_from_signal,
    size_from_risk,
    to_live_signal,
)


def _sig(side="BUY", entry=100.0, sl_dist=2.0, tp_dist=6.0, time="2026-11-10 10:15"):
    return {"side": side, "entry": entry, "sl_dist": sl_dist, "tp_dist": tp_dist, "time": time}


def _limits(**over):
    base = dict(max_order_quantity=10_000, max_order_notional=500_000.0,
                max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)
    base.update(over)
    return RiskLimits(**base)


@pytest.fixture
def ctx(tmp_path):
    return dict(state=RiskState(), limits=_limits(), kill=KillSwitch(tmp_path / "kill.json"),
                safe=SafeMode(), halt=HaltHandler(), available_funds=1_000_000.0)


def _run(sig, ctx, market=None, **over):
    kw = dict(symbol="INFY", strategy_id="engulfing", equity=1_000_000.0, risk_fraction=0.0025)
    kw.update(ctx)
    kw.update(market or {})
    kw.update(over)
    return gate_signal(sig, **kw)


def test_buy_levels_sit_below_and_above_entry():
    assert levels_from_signal(_sig("BUY")) == (100.0, 98.0, 106.0)


def test_sell_levels_are_mirrored():
    assert levels_from_signal(_sig("SELL")) == (100.0, 102.0, 94.0)


def test_non_positive_distance_rejected():
    with pytest.raises(ValueError):
        levels_from_signal(_sig(sl_dist=0.0))


def test_size_is_risk_money_over_stop_distance():
    # 1,000,000 * 0.25% = 2,500 money at risk; stop is 2.0 per share -> 1,250 shares
    assert size_from_risk(1_000_000.0, 0.0025, 100.0, 98.0) == 1250


def test_size_zero_for_bad_inputs():
    assert size_from_risk(0.0, 0.0025, 100.0, 98.0) == 0
    assert size_from_risk(1_000_000.0, 0.0, 100.0, 98.0) == 0
    assert size_from_risk(1_000_000.0, 0.10, 100.0, 98.0) == 0  # above the 5% cap
    assert size_from_risk(1_000_000.0, 0.0025, 100.0, 100.0) == 0


def test_live_signal_has_contract_keys():
    live = to_live_signal(_sig("BUY"), 1_000_000.0, 0.0025)
    assert live["qty"] == 1250 and live["sl"] == 98.0 and live["tp"] == 106.0


MARKET = dict(adv_shares=5_000_000.0, prev_close=100.0, circuit_pct=20.0)


def test_good_signal_is_approved(ctx):
    res = _run(_sig("BUY"), ctx, market=MARKET)
    assert res.ok, res.reason
    assert res.intent.qty == 1250
    assert res.intent.side == "BUY"
    assert res.intent.kind == "entry"
    assert res.decision.approved


def test_missing_adv_fails_closed(ctx):
    res = _run(_sig("BUY"), ctx)
    assert not res.ok
    assert "ADV unknown" in res.reason


def test_kill_switch_blocks_entry(ctx):
    ctx["kill"].trip("test halt")
    res = _run(_sig("BUY"), ctx)
    assert not res.ok
    assert "kill switch" in res.reason


def test_halt_all_blocks_entry(ctx):
    ctx["safe"].set(Mode.HALT_ALL, "test")
    res = _run(_sig("BUY"), ctx)
    assert not res.ok
    assert "halt-all" in res.reason


def test_reduce_only_blocks_entry(ctx):
    ctx["safe"].set(Mode.REDUCE_ONLY, "test")
    assert not _run(_sig("BUY"), ctx).ok


def test_insufficient_funds_rejected(ctx):
    res = _run(_sig("BUY"), ctx, available_funds=1_000.0)
    assert not res.ok
    assert "insufficient funds" in res.reason


def test_duplicate_intent_rejected(ctx):
    first = _run(_sig("BUY"), ctx)
    ctx["state"].seen_ids.add(first.intent.intent_id)
    res = _run(_sig("BUY"), ctx)
    assert not res.ok
    assert "duplicate" in res.reason


def test_zero_size_rejected_before_gate(ctx):
    res = _run(_sig("BUY"), ctx, equity=0.0)
    assert not res.ok
    assert res.intent is None


def test_bad_signal_rejected_without_crash(ctx):
    res = _run({"side": "BUY"}, ctx)
    assert not res.ok
    assert res.intent is None


def test_no_signal_rejected(ctx):
    res = _run(None, ctx)
    assert not res.ok
