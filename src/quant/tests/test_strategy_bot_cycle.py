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

    def cycle_for(self, symbol, now, allocation_pct=None):
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


def _bot(tmp_path, strategy, store=None, restore_at=None, bars=None, loss_ceiling=0.01, portfolio_ceiling=0.03,
         symbol_allocation=None):
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
                      cycle_portfolio_loss_ceiling=(0.05 if symbol_allocation is not None else portfolio_ceiling),
                      # owner tree: 5 % portfolio, symbol cap 4 %; the allocator derives INFY's 4 % from the screen
                      cycle_symbol_allocation_pct=symbol_allocation)
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
    # probe 0.10 % of 1,000,000 = 1,000 INR over the sizing stop: max(ATR 1.0, 2.00 + 0.05 % x 100 = 2.05) = 2.05
    assert _child_qtys(audit) == [487]  # floor(1000 / 2.05) = 487
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
    assert _child_qtys(audit) == [487]
    bot.cycle(NOW + timedelta(minutes=5))
    assert _parent(bot).paused is True
    assert _child_qtys(audit) == [487]
    bot.cycle(NOW + timedelta(minutes=10))
    assert _parent(bot).paused is False
    b = _parent(bot).ladder.orders[1]
    # B at 99: stop = max(1.0, 1.00 + 0.0495) = 1.0495 -> 1,000 / 1.0495 = 952 shares
    assert b.reason == "ladder" and b.carry == 0 and b.qty == 952
    assert _child_qtys(audit) == [487, 500]  # the child is capped by the bar's volume (POV), not by the ladder


def test_partial_fill_is_carried_into_the_next_ocl(tmp_path):
    """Probe 500 shares, only 100 fill (thin bar). The stop-out carries the 400 unfilled into OCL B:
    the next order is 387 carried + 952 fresh (0.10 %) = 1339 shares."""
    strat = ScriptedCycle("scripted", [
        _view(_result([("aggressive", "A", 100.0, T_NEW)]), T_NEW),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT)]), T_NEXT),
        _view(_result([("aggressive", "A", 100.0, T_NEW), ("invalidated", "A", 98.5, T_NEXT),
                       ("aggressive", "B", 99.0, T_NEXT)]), T_NEXT),
    ])
    bot, audit = _bot(tmp_path, strat, bars=SmallBars())
    bot.cycle(NOW)
    assert _child_qtys(audit) == [100]
    assert _parent(bot).remaining == 387  # probe 487 - 100 filled
    bot.cycle(NOW + timedelta(minutes=5))
    assert _parent(bot).paused is True
    bot.cycle(NOW + timedelta(minutes=10))
    b = _parent(bot).ladder.orders[1]
    assert b.carry == 387 and b.qty == 387 + 952
    assert _parent(bot).remaining == 387 + 952 - 100  # carried + fresh, counted once, minus the child sent now
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
    assert _parent(bot1).remaining == 387

    day2 = ScriptedCycle("scripted", [_view(_result([("aggressive", "A", 100.0, T_NEW)]), T_DAY2)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY, bars=SmallBars())
    assert _key() in bot2._positions  # the open position came back
    assert _parent(bot2).remaining == 387
    assert len(_parent(bot2).ladder.orders) == 1  # ladder state came back, no second order
    bot2.cycle(NEXT_DAY)
    assert _child_qtys(audit2) == [100]
    assert _parent(bot2).remaining == 287  # 387 restored, minus the 100 sent after the restart


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
    assert _parent(bot1).remaining == 387

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
    """Probe 487 sh x 2.05 stop = ~998 INR planned risk. A ceiling of 0.05 % (500 INR) refuses it."""
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
    assert _parent(bot).ladder.carry == 0 and _parent(bot).remaining == 387
    bot.cycle(NOW + timedelta(minutes=5))
    assert _parent(bot).ladder.carry == 387  # the 387 unfilled shares of A are released
    bot.cycle(NOW + timedelta(minutes=10))
    b = _parent(bot).ladder.orders[1]
    assert b.carry == 387 and b.qty == 500   # planned 387 + 952 = 1339, placed 500 (the cap)
    assert _parent(bot).ladder.carry == 839  # the blocked 839 are carried again
    clamps = [p for k, p in audit.records if k == "ladder_clamp"]
    assert [c["carried"] for c in clamps] == [839]
    ols.reset()


def test_a_small_cap_clamps_the_first_probe_and_keeps_the_rest(tmp_path, monkeypatch):
    """Cap 50 shares: the 487-share probe is clamped to 50 (not refused), and 437 are carried."""
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
    assert _parent(bot).ladder.carry == 437
    assert _parent(bot).ladder.orders[0].qty == 50
    assert [p["carried"] for k, p in audit.records if k == "ladder_clamp"] == [437]
    ols.reset()


def test_portfolio_ceiling_refuses_a_probe_that_would_exceed_it(tmp_path):
    """Probe = 487 sh x 2.05 stop = ~998 INR planned. A 0.05 % portfolio cap is 500 INR, so it is refused."""
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
    assert _child_qtys(audit) == [487]  # ~998 INR planned, inside the 3 % (3,000 INR) cap


def test_adv_cap_is_one_percent_of_average_daily_volume_and_fails_safe():
    from quant.execution.strategy_bot import _adv_cap_qty
    assert _adv_cap_qty(0.01, 250_000) == 2500         # 1 % of 2.5 lakh shares
    assert _adv_cap_qty(0.01, 99) == 0                 # tiny volume: nothing allowed
    assert _adv_cap_qty(None, 250_000) > 10**9         # not set: this helper does not cap (gate still refuses)


# ---- owner tree (2026-11-20): budgets come from risk_tree.plan, not the old ladder -----------------
def _tree_result(events, decision_events):
    """A replay result whose tree decisions come from risk_tree.plan over decision_events (4 % allocation)."""
    from quant.strategies.risk_tree import plan
    r = _result(events)
    r.tree_decisions = plan(decision_events, 4.0)
    return r


def test_tree_leaf_touch_is_sized_from_its_budget(tmp_path):
    """4 % allocation, 60/40: the aggressive leaf gets 0.096 % of 10 lakh = 960 INR. Stop 2.05 -> 468 shares."""
    r = _tree_result(
        [("aggressive", "A", 100.0, T_NEW)],
        [("htf_ocl", "H", T_NEW), ("ltf_ocl", "A", T_NEW), ("ltf_agg_touch", "A", T_NEW)])
    strat = ScriptedCycle("scripted", [_view(r, T_NEW)])
    bot, audit = _bot(tmp_path, strat, symbol_allocation=4.0)
    bot.cycle(NOW)
    assert _child_qtys(audit) == [468]  # floor(960 / 2.05)
    assert _parent(bot).ladder.orders[0].reason == "leaf_aggressive"  # the stage name from the tree decision


def test_tree_touch_without_a_decision_is_skipped_and_logged(tmp_path):
    r = _tree_result([("aggressive", "A", 100.0, T_NEW)], [("htf_ocl", "H", T_NEW)])  # no ltf_ocl / touch
    strat = ScriptedCycle("scripted", [_view(r, T_NEW)])
    bot, audit = _bot(tmp_path, strat, symbol_allocation=4.0)
    bot.cycle(NOW)
    assert _child_qtys(audit) == []
    assert any(k == "tree_refused" and p.get("reason") == "no_decision" for k, p in audit.records)


def test_tree_refused_leaf_sends_nothing(tmp_path):
    """The LTF OCL's slot was refused (its HTF gate had no slot): the touch has a refused decision."""
    r = _tree_result([("aggressive", "A", 100.0, T_NEW)],
                     [("ltf_ocl", "A", T_NEW), ("ltf_agg_touch", "A", T_NEW)])  # no htf_ocl: refused
    strat = ScriptedCycle("scripted", [_view(r, T_NEW)])
    bot, audit = _bot(tmp_path, strat, symbol_allocation=4.0)
    bot.cycle(NOW)
    assert _child_qtys(audit) == []
    assert any(k == "tree_refused" for k, _ in audit.records)


def test_unfilled_shares_carry_from_a_closed_htf_ocl_into_the_next_one(tmp_path):
    """Owner tree: unfilled shares roll forward to the next HTF OCL of the same MTF setup.
    HTF OCL 1: leaf budget 960 INR -> 468 sh; 100 fill, then the HTF OCL is invalidated -> 368 carried.
    HTF OCL 2: leaf budget 960 INR at stop 1.0495 -> 914 sh; order = 914 + 368 carried = 1282 sh."""
    from quant.strategies.risk_tree import plan
    from quant.strategies.cycle import CycleResult
    K2 = pd.Timestamp("2026-11-16 17:00")
    decisions = plan([
        ("htf_ocl", str(HTF_KEY), HTF_KEY), ("ltf_ocl", "A", T_NEW), ("ltf_agg_touch", "A", T_NEW),
        ("htf_invalidated", T_NEXT), ("htf_ocl", str(K2), K2), ("ltf_ocl", "B", T_NEXT),
        ("ltf_agg_touch", "B", T_NEXT)], 4.0)

    def res(key, status, events):
        r = CycleResult(kind="egpattern_open", status=status, htf_low=HTF_LOW, entries=[], avg_entry=None,
                        mode="target", target=104.2, htf_key_time=key, ltf_paused=False, events=list(events))
        r.tree_decisions = decisions
        return r

    r1_open = res(HTF_KEY, "open", [("aggressive", "A", 100.0, T_NEW)])
    strat = ScriptedCycle("scripted", [CycleView(results=[r1_open], newest_ltf_time=T_NEW, atr=1.0)])
    bot, audit = _bot(tmp_path, strat, bars=SmallBars(), symbol_allocation=4.0)
    bot.cycle(NOW)
    assert _child_qtys(audit) == [100]
    assert _parent(bot).ladder.orders[0].qty == 468
    assert _parent(bot).remaining == 368

    r1_done = res(HTF_KEY, "htf_invalidated", [("aggressive", "A", 100.0, T_NEW)])
    r2_open = res(K2, "open", [("aggressive", "B", 99.0, T_NEXT)])
    strat.views = [CycleView(results=[r1_done, r2_open], newest_ltf_time=T_NEXT, atr=1.0)]
    bot.cycle(NOW + timedelta(minutes=10))
    assert "mtf_carry" in [k for k, _ in audit.records]
    assert bot._mtf_carry == {} or sum(bot._mtf_carry.values()) == 0  # consumed by the new order
    key2 = f"INFY|scripted|BUY|cycle|{K2}"
    p2 = bot._parents[key2]
    assert p2.ladder.orders[-1].qty == 914 + 368  # fresh leaf budget at stop 1.0495, plus carried shares
    assert p2.ladder.orders[-1].carry == 368


def test_carry_is_not_consumed_when_the_new_touch_is_refused(tmp_path):
    """No budget on the next touch: the carried shares stay in the pool, not lost."""
    from quant.strategies.risk_tree import plan
    from quant.strategies.cycle import CycleResult
    K2 = pd.Timestamp("2026-11-16 17:00")
    decisions = plan([
        ("htf_ocl", str(HTF_KEY), HTF_KEY), ("ltf_ocl", "A", T_NEW), ("ltf_agg_touch", "A", T_NEW),
        ("htf_invalidated", T_NEXT), ("htf_ocl", str(K2), K2)], 4.0)  # no LTF OCL in the second HTF cycle

    def res(key, status, events):
        r = CycleResult(kind="egpattern_open", status=status, htf_low=HTF_LOW, entries=[], avg_entry=None,
                        mode="target", target=104.2, htf_key_time=key, ltf_paused=False, events=list(events))
        r.tree_decisions = decisions
        return r

    strat = ScriptedCycle("scripted", [CycleView(results=[res(HTF_KEY, "open", [("aggressive", "A", 100.0, T_NEW)])],
                                                 newest_ltf_time=T_NEW, atr=1.0)])
    bot, audit = _bot(tmp_path, strat, bars=SmallBars(), symbol_allocation=4.0)
    bot.cycle(NOW)
    strat.views = [CycleView(results=[res(HTF_KEY, "htf_invalidated", [("aggressive", "A", 100.0, T_NEW)]),
                                      res(K2, "open", [("aggressive", "B", 99.0, T_NEXT)])],
                             newest_ltf_time=T_NEXT, atr=1.0)]
    bot.cycle(NOW + timedelta(minutes=10))
    assert bot._mtf_carry.get(str(HTF_KEY)) == 368  # still waiting in the pool
    assert _child_qtys(audit) == [100]  # nothing new was sent


def test_symbol_allocation_comes_from_the_portfolio_allocator():
    """Owner tree: 5 % portfolio, 4 % symbol cap. One symbol gets the cap; more symbols share the 5 %."""
    from quant.execution.strategy_bot import symbol_allocations
    assert symbol_allocations(["A"], 5.0, 4.0) == pytest.approx({"A": 4.0})
    two = symbol_allocations(["A", "B"], 5.0, 4.0)
    assert two == pytest.approx({"A": 2.5, "B": 2.5})
    five = symbol_allocations(["A", "B", "C", "D", "E"], 5.0, 4.0)
    assert sum(five.values()) == pytest.approx(5.0) and all(v == pytest.approx(1.0) for v in five.values())
    assert symbol_allocations([], 5.0, 4.0) == {}


def test_tree_cycle_without_a_screen_allocation_fails_closed(tmp_path):
    """No allocation for the symbol (not on the screen): the tree touch sends nothing and is logged."""
    r = _tree_result(
        [("aggressive", "A", 100.0, T_NEW)],
        [("htf_ocl", "H", T_NEW), ("ltf_ocl", "A", T_NEW), ("ltf_agg_touch", "A", T_NEW)])
    strat = ScriptedCycle("scripted", [_view(r, T_NEW)])
    bot, audit = _bot(tmp_path, strat)  # no symbol cap configured -> the allocator gives no allocation
    bot.cycle(NOW)
    assert _child_qtys(audit) == []
    assert any(kind == "tree_no_allocation" for kind, _ in audit.records)
