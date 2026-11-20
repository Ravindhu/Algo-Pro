"""Flow stage: Strategy bot cycle, Risk gate, Approved order queue (cycle rules, owner 2026-11-19).
Status: TESTS for the cycle wiring in strategy_bot.py (paper). Real gate, real POV executor and the
PaperBroker; only the bar feed and the cycle strategy are scripted fakes.

Rules checked here:
- one parent per HTF cycle; the first fresh trigger sizes it through the risk gate;
- no entry from an older bar (freshness: newest closed LTF bar only);
- an LTF stop-out pauses slicing; the next LTF touch resumes it (same quantity, also after a restart);
- a cycle that has ended is never opened again after a restart;
- cycle parents are not dropped by the max_cycles age limit;
- cycle positions: no breakeven; trail from entry only after a conservative touch; target None until N is set.
"""
import json

import pytest
from datetime import datetime, timedelta

import pandas as pd
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import PaperBroker
from quant.execution.position_store import PositionStore
from quant.execution.strategy_bot import BotConfig, PaperSender, StrategyBot
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch
from quant.strategies.cycle import CycleResult
from quant.strategies.engulf_sequence_strategy import CycleView


@pytest.fixture(autouse=True)
def _test_only_owner_caps(tmp_path, monkeypatch):
    """Owner caps in config/risk_limits.yaml (10 shares, 10,000 INR) would refuse these test
    entries. Point the gate at a test-only file with large caps. The real file is not changed."""
    from quant.core import owner_limits_store as ols
    p = tmp_path / "owner_caps_for_tests.yaml"
    p.write_text("max_order_quantity: 1000000\nmax_order_notional: 100000000\n"
                 "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", p)
    monkeypatch.setattr(ols, "_loaded_from", None)
    monkeypatch.setattr(ols, "_cached", {})
    yield

NOW = datetime(2026, 11, 17, 10, 15)
T_OLD = pd.Timestamp("2026-11-17 10:00")
T_NEW = pd.Timestamp("2026-11-17 10:05")
T_NEXT = pd.Timestamp("2026-11-17 10:10")
NEXT_DAY = NOW + timedelta(days=1)
T_DAY2 = pd.Timestamp("2026-11-18 10:05")
HTF_KEY = pd.Timestamp("2026-11-16 15:15")
HTF_LOW = 98.0  # ladder: equity 1,000,000; probe 0.10 % at 100 -> 500 shares (R = 2)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def kinds(self):
        return [k for k, _ in self.records]


class Bars:
    VOLUME = 5000.0  # POV 10 % -> 500 shares per child

    def bars(self, symbol, now):
        return pd.DataFrame({"open": [100.0] * 40, "high": [100.0] * 40, "low": [100.0] * 40,
                             "close": [100.0] * 40, "volume": [self.VOLUME] * 40})

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class ScriptedCycle:
    """A cycle strategy that returns one scripted CycleView per bot cycle (None when the script is empty)."""
    is_cycle = True

    def __init__(self, name, views):
        self.name = name
        self.views = list(views)

    def cycle_for(self, symbol, now):
        return self.views.pop(0) if self.views else None


# target N = 2.1 (owner): avg 100 + 2.1 x (100 - 98) = 104.2. target_pending (N unset) is tested separately.
def _result(events=(), mode="target", target=104.2, status="open"):
    """A replay result. events: (kind, ocl_id, price, time) in order, kind = aggressive | conservative | invalidated.
    target 104.2 = avg 100 + N x R with N = 2.1 (owner); mode target_pending is tested separately."""
    return CycleResult(kind="egpattern_open", status=status, htf_low=HTF_LOW, entries=[],
                       avg_entry=None, mode=mode, target=target, htf_key_time=HTF_KEY,
                       ltf_paused=False, events=list(events))


def _view(result, newest):
    return CycleView(results=[result], newest_ltf_time=newest, atr=1.0)


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _bot(tmp_path, strategy, store=None, restore_at=None, bars=None, loss_ceiling=0.01, portfolio_ceiling=0.03):
    screen = tmp_path / "latest.json"
    as_of = restore_at or NOW  # a restarted bot reads a fresh screen (the live screen is refreshed each cycle)
    screen.write_text(json.dumps({"as_of": as_of.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources))
    audit = Audit()
    bot = StrategyBot(cfg=cfg, strategies=[strategy], bars=bars or Bars(), sender=PaperSender(PaperBroker()),
                      audit=audit, api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
                      kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
                      state=RiskState(), position_store=store,
                      cycle_loss_ceiling=loss_ceiling,  # owner value 1 % of equity (tests may lower it)
                      cycle_portfolio_loss_ceiling=portfolio_ceiling)  # owner value 3 % of equity
    if restore_at is not None:
        bot.restore_positions(restore_at)  # before startup recon, as in the live runner
    bot.run_startup_recon(None)
    return bot, audit


class SmallBars(Bars):
    VOLUME = 1000.0  # POV 10 % -> 100 shares per child: partial fills, so carry can be tested


def _key():
    return f"INFY|scripted|BUY|cycle|{HTF_KEY}"


def _child_qtys(audit):
    return [p["qty"] for k, p in audit.records if k == "child_sent"]


def _parent(bot):
    return bot._parents[_key()]


def test_first_fresh_trigger_opens_one_cycle_parent_with_hard_htf_stop(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() in bot._parents
    pos = bot._positions[_key()]
    assert pos.stop == HTF_LOW
    assert pos.breakeven_enabled is False
    assert pos.trail_from_start is False
    assert pos.target == pytest.approx(104.2)  # target = avg + N x R, N = 2.1 (owner)
    assert _child_qtys(audit) == [500]  # probe 0.10 % of 1,000,000 over 2 INR = 500 shares
    assert _parent(bot).ladder.orders[0].reason == "probe_aggressive"


def test_entry_on_older_bar_does_not_open_a_cycle(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_OLD)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []


def test_target_pending_refuses_the_cycle_fail_closed(tmp_path):
    """Owner rule 11.7: an aggressive-only cycle with no N set has no exit rule, so it is never opened."""
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)],
                                                     mode="target_pending", target=None), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []
    assert any(k == "parent_skipped" and p.get("reason") == "target N not set" for k, p in audit.records)


def test_no_loss_ceiling_means_no_cycle_order(tmp_path):
    """Fail closed: with the ceiling not set, the ladder cannot size anything."""
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot._cycle_loss_ceiling = None
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []


def test_invalidation_then_next_ocl_adds_fresh_quantity_and_resumes(tmp_path):
    """Probe filled, stop-out pauses, the next OCL below adds 0.10 % at its aggressive level (no unfilled left to carry)."""
    strat = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT)]), T_NEXT),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT),
                       ("aggressive", "B", 99.0, T_NEXT)]), T_NEXT),
    ])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _child_qtys(audit) == [500]
    bot.cycle(NOW + timedelta(minutes=5))
    assert _parent(bot).paused is True
    assert _child_qtys(audit) == [500]
    bot.cycle(NOW + timedelta(minutes=10))
    assert _parent(bot).paused is False
    b = _parent(bot).ladder.orders[1]
    assert b.reason == "ladder" and b.carry == 0 and b.qty == 1000  # 0.10 % of 1,000,000 over 1 INR
    assert _child_qtys(audit) == [500, 500]


def test_partial_fill_is_carried_into_the_next_ocl(tmp_path):
    """Probe 500 shares, only 100 fill (thin bar). The stop-out carries the 400 unfilled into OCL B:
    the next order is 400 carried + 1000 fresh (0.10 %) = 1400 shares."""
    strat = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT)]), T_NEXT),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT),
                       ("aggressive", "B", 99.0, T_NEXT)]), T_NEXT),
    ])
    bot, audit = _bot(tmp_path, strat, bars=SmallBars())
    bot.cycle(NOW)
    assert _child_qtys(audit) == [100]
    assert _parent(bot).remaining == 400
    bot.cycle(NOW + timedelta(minutes=5))
    assert _parent(bot).paused is True
    bot.cycle(NOW + timedelta(minutes=10))
    b = _parent(bot).ladder.orders[1]
    assert b.carry == 400 and b.qty == 1400
    assert _parent(bot).remaining == 400 + 1000 - 100  # carried + fresh, counted once, minus the child sent now
    assert _child_qtys(audit) == [100, 100]


def test_touch_at_or_below_the_htf_low_does_not_add_quantity(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT),
                       ("aggressive", "B", HTF_LOW, T_NEXT)]), T_NEXT),
    ])
    bot, audit = _bot(tmp_path, strat, bars=SmallBars())
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    assert len(_parent(bot).ladder.orders) == 1  # no order at the hard stop
    assert _child_qtys(audit) == [100]  # the closed order has nothing left to fill


def test_restart_mid_cycle_keeps_the_remaining_quantity_and_ladder(tmp_path):
    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot1, audit1 = _bot(tmp_path, day1, store=store, bars=SmallBars())
    bot1.cycle(NOW)
    assert _child_qtys(audit1) == [100]
    assert _parent(bot1).remaining == 400

    day2 = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_DAY2)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY, bars=SmallBars())
    assert _key() in bot2._positions  # the open position came back
    assert _parent(bot2).remaining == 400
    assert len(_parent(bot2).ladder.orders) == 1  # ladder state came back, no second order
    bot2.cycle(NEXT_DAY)
    assert _child_qtys(audit2) == [100]
    assert _parent(bot2).remaining == 300


def test_restart_after_stop_out_keeps_the_parent_paused_then_resumes(tmp_path):
    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT)]), T_NEXT),
    ])
    bot1, _ = _bot(tmp_path, day1, store=store, bars=SmallBars())
    bot1.cycle(NOW)
    bot1._finish_exit(_key(), bot1._positions[_key()], 98.0, "stop", 98.0)  # the stop closed the position
    bot1.cycle(NOW + timedelta(minutes=5))  # invalidation processed and saved
    assert _parent(bot1).paused is True
    assert _parent(bot1).remaining == 400

    day2 = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT)]), T_DAY2),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT),
                       ("aggressive", "B", 99.0, T_DAY2)]), T_DAY2),
    ])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY, bars=SmallBars())
    assert _parent(bot2).paused is True
    bot2.cycle(NEXT_DAY)
    assert _child_qtys(audit2) == []  # paused and the touch is not there yet
    bot2.cycle(NEXT_DAY + timedelta(minutes=5))
    assert _child_qtys(audit2) == [100]  # the next OCL touch resumes slicing (carry + fresh)


def test_conservative_touch_switches_position_to_atr_trail(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("conservative", "A", 99.0, T_NEXT)],
                      mode="trail", target=None), T_NEXT),  # trail mode: no fixed target (rules 11.7)
    ])
    bot, _ = _bot(tmp_path, strat)
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    pos = bot._positions[_key()]
    assert pos.trail_from_start is True
    assert pos.target is None
    assert pos.breakeven_enabled is False
    assert _parent(bot).trail is True


def test_restart_after_a_cycle_ended_never_reopens_it(tmp_path):
    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot1, _ = _bot(tmp_path, day1, store=store)
    bot1.cycle(NOW)
    bot1._close(_key(), "HTF invalidated: unfilled quantity dropped")

    day2 = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_DAY2)]), T_DAY2)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY)
    assert _key() not in bot2._parents
    bot2.cycle(NEXT_DAY)
    assert _key() not in bot2._parents


def test_loss_ceiling_refusal_sends_nothing_and_is_audited(tmp_path):
    """Probe 500 sh x 2 INR = 1000 INR planned risk. A ceiling of 0.05 % (500 INR) refuses it."""
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat, loss_ceiling=0.0005)
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []
    refusals = [p for k, p in audit.records if k == "ladder_refused"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "loss_ceiling"
    assert refusals[0]["ceiling_inr"] == pytest.approx(500.0)


def test_shares_blocked_by_a_cap_are_carried_not_dropped(tmp_path, monkeypatch):
    """Owner 2026-11-19: shares a cap blocks are carried, like unfilled shares. Cap = 500 shares.
    Probe A plans 500 and only 100 fill (thin bar), so 400 are unfilled. OCL B plans 400 carried +
    1000 fresh = 1400, but the cap places 500 and blocks 900. The 900 must be carried, never dropped."""
    from quant.core import owner_limits_store as ols
    capped = tmp_path / "owner_caps_capped.yaml"
    capped.write_text("max_order_quantity: 500\nmax_order_notional: 100000000\n"
                      "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", capped)
    ols.reset()
    strat = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT)]), T_NEXT),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT),
                       ("aggressive", "B", 99.0, T_NEXT)]), T_NEXT),
    ])
    bot, audit = _bot(tmp_path, strat, bars=SmallBars())
    bot.cycle(NOW)
    assert _child_qtys(audit) == [100]
    assert _parent(bot).ladder.carry == 0 and _parent(bot).remaining == 400
    bot.cycle(NOW + timedelta(minutes=5))
    assert _parent(bot).ladder.carry == 400  # the 400 unfilled shares of A are released
    bot.cycle(NOW + timedelta(minutes=10))
    b = _parent(bot).ladder.orders[1]
    assert b.carry == 400 and b.qty == 500   # planned 1400, placed 500 (the cap)
    assert _parent(bot).ladder.carry == 900  # the blocked 900 are carried again
    clamps = [p for k, p in audit.records if k == "ladder_clamp"]
    assert [c["carried"] for c in clamps] == [900]
    ols.reset()


def test_a_small_cap_clamps_the_first_probe_and_keeps_the_rest(tmp_path, monkeypatch):
    """Cap 50 shares: the 500-share probe is clamped to 50 (not refused), and 450 are carried."""
    from quant.core import owner_limits_store as ols
    capped = tmp_path / "owner_caps_50.yaml"
    capped.write_text("max_order_quantity: 50\nmax_order_notional: 100000000\n"
                      "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", capped)
    ols.reset()
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _child_qtys(audit) == [50]
    assert _parent(bot).ladder.carry == 450
    assert _parent(bot).ladder.orders[0].qty == 50
    assert [p["carried"] for k, p in audit.records if k == "ladder_clamp"] == [450]
    ols.reset()


def test_portfolio_ceiling_refuses_a_probe_that_would_exceed_it(tmp_path):
    """Probe = 500 sh x (100 - 98) = 1000 INR planned. A 0.05 % portfolio cap is 500 INR, so it is refused."""
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat, portfolio_ceiling=0.0005)
    bot.cycle(NOW)
    assert _child_qtys(audit) == []
    refusals = [p for k, p in audit.records if k == "ladder_refused"]
    assert refusals and refusals[0]["reason"] == "portfolio_loss_ceiling"


def test_portfolio_ceiling_unset_sends_no_ladder_order(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat, portfolio_ceiling=None)
    bot.cycle(NOW)
    assert _child_qtys(audit) == []


def test_portfolio_ceiling_allows_a_probe_within_three_percent(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW)])
    bot, audit = _bot(tmp_path, strat, portfolio_ceiling=0.03)
    bot.cycle(NOW)
    assert _child_qtys(audit) == [500]


def test_adv_cap_is_one_percent_of_average_daily_volume_and_fails_safe():
    from quant.execution.strategy_bot import _adv_cap_qty
    assert _adv_cap_qty(0.01, 250_000) == 2500         # 1 % of 2.5 lakh shares
    assert _adv_cap_qty(0.01, 99) == 0                 # tiny volume: nothing allowed
    assert _adv_cap_qty(None, 250_000) > 10**9         # not set: this helper does not cap (gate still refuses)
