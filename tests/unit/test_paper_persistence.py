"""Paper exits go through the risk gate, and positions survive a restart. Paper only, 2026-11-08.

Hand-checked with fabricated bars. Uses the real check_intent gate and the real risk_limits.yaml for the
gate cases. Each restart test builds fresh objects and restores them from the state file.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.core.config import RiskLimits
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.monitor.safe_mode import SafeMode
from quant.strategies.ocl_sell_fail import Handoff
from quant.strategies.positions import PaperPositionManager
from quant.strategies.protect import Protection
from quant.strategies.scanner import ListAudit
from quant.strategies.state_store import STATE_VERSION, load_state, restore_all, save_state

ROOT = Path(__file__).resolve().parents[2]
LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk_limits.yaml")
DAY = "2026-11-09"


class FakeStrat:
    name = "ocl"
    symbol = "RELIANCE"
    htf_tf = "15minute"
    ltf_tf = "5minute"

    def __init__(self):
        self.p = SimpleNamespace(protection=Protection(atr_trail_enabled=False), atr_period=14)
        self.exit_flag = False
        self.exits = []
        self.entered = 0
        self.level = 1.0

    def exit_check(self, frames):
        return self.exit_flag

    def on_exit(self, reason, price):
        self.exits.append((reason, price))

    def on_entry(self):
        self.entered += 1

    def signal_frames(self, frames):
        return None

    def state(self):
        return {"level": self.level, "used": [1, 2]}

    def restore(self, d):
        self.level = d["level"]


def sig(time="2026-11-09 09:10:00"):
    return {"entry_ref": 100.0, "trigger": 97.0, "htf_sl": 96.0, "sl_price": 94.0,
            "tp_dist": None, "exit_mode": "DYNAMIC", "time": time}


def ltf_frames(rows, start="09:15"):
    idx = pd.date_range(f"{DAY} {start}", periods=len(rows) + 1, freq="5min")
    data = list(rows) + [(rows[-1][3],) * 4]
    o, h, l, c = zip(*data)
    return {"5minute": pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0}, index=idx),
            "15minute": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                                     index=pd.date_range(f"{DAY} 09:15", periods=1, freq="15min"))}


BENIGN = (100, 100.5, 99.5, 100.0)
STOP_OUT = (100, 100.5, 99.5, 95.5)      # close below HTF floor 96, low above broker stop 94


def approve(intent):
    return SimpleNamespace(sent=True, reasons=())


def real_gate_submit(tmp_path, calls=None):
    """Exit submit through the real gate, as the runtime wires it (no open entries, funds available)."""
    kill = KillSwitch(tmp_path / "KILL")
    safe = SafeMode()

    def submit(intent):
        if calls is not None:
            calls.append(intent)
        d = check_intent(intent, RiskState(open_notional=0.0, open_symbols=set()), LIMITS, kill, safe,
                         available_funds=1_000_000.0)
        return SimpleNamespace(sent=d.approved, reasons=d.reasons)

    return submit


def open_pos(mgr, strat, qty=10):
    return mgr.open(key="k1", strategy=strat, symbol="RELIANCE", qty=qty, sig=sig(), bar_time=sig()["time"])


# ---------- exits through the gate ----------

def test_exit_intent_is_a_sell_exit_with_adv_and_qty():
    strat = FakeStrat()
    calls = []

    def submit(intent):
        calls.append(intent)
        return SimpleNamespace(sent=True, reasons=())

    mgr = PaperPositionManager(ListAudit(), submit=submit)
    open_pos(mgr, strat)
    mgr.update({"RELIANCE": ltf_frames([STOP_OUT])}, adv_shares={"RELIANCE": 5_000_000.0})
    assert len(calls) == 1
    intent = calls[0]
    assert intent.kind == "exit" and intent.side == "SELL"
    assert intent.qty == 10 and intent.ref_price == 95.5
    assert intent.adv_shares == 5_000_000.0
    assert strat.exits == [("SL", 95.5)]


def test_rejected_exit_stays_pending_and_is_retried_until_approved():
    strat = FakeStrat()
    audit = ListAudit()
    answers = iter([False, False, True])

    def submit(intent):
        ok = next(answers)
        return SimpleNamespace(sent=ok, reasons=() if ok else ("safe mode: reduce-only",))

    mgr = PaperPositionManager(audit, submit=submit)
    open_pos(mgr, strat)
    frames = ltf_frames([STOP_OUT])

    mgr.update({"RELIANCE": frames})                      # rejected
    assert "k1" in mgr.open_positions()
    assert mgr.open_positions()["k1"].pending["reason"] == "SL"
    assert strat.exits == []                              # no close before the gate says yes
    assert any(r["kind"] == "exit_pending" for r in audit.records)

    mgr.update({"RELIANCE": frames})                      # still rejected, no new bars needed
    assert "k1" in mgr.open_positions() and strat.exits == []

    mgr.update({"RELIANCE": frames})                      # approved now
    assert "k1" not in mgr.open_positions()
    assert strat.exits == [("SL", 95.5)]
    assert mgr.closed_trades[-1]["reason"] == "SL" and mgr.closed_trades[-1]["mode"] == "PAPER"


def test_real_gate_approves_a_stop_exit(tmp_path):
    strat = FakeStrat()
    calls = []
    mgr = PaperPositionManager(ListAudit(), submit=real_gate_submit(tmp_path, calls))
    open_pos(mgr, strat)
    mgr.update({"RELIANCE": ltf_frames([STOP_OUT])})
    assert strat.exits == [("SL", 95.5)]
    assert calls[0].kind == "exit"


def test_exit_never_skipped_while_pending():
    """While an exit is pending, later bars are not processed, so a later bar cannot change the exit."""
    strat = FakeStrat()
    answers = iter([False, True])
    mgr = PaperPositionManager(ListAudit(), submit=lambda i: SimpleNamespace(
        sent=next(answers), reasons=()))
    open_pos(mgr, strat)
    mgr.update({"RELIANCE": ltf_frames([STOP_OUT])})      # pending SL at 95.5
    mgr.update({"RELIANCE": ltf_frames([STOP_OUT, BENIGN])})
    assert strat.exits == [("SL", 95.5)]


def test_missing_submit_refuses_to_close_silently():
    strat = FakeStrat()
    mgr = PaperPositionManager(ListAudit())               # no submit function
    open_pos(mgr, strat)
    with pytest.raises(RuntimeError):
        mgr.update({"RELIANCE": ltf_frames([STOP_OUT])})
    assert "k1" in mgr.open_positions() and strat.exits == []


# ---------- persistence across restart ----------

def test_restart_keeps_open_position_and_exit_still_works(tmp_path):
    path = tmp_path / "state.json"
    strat1 = FakeStrat()
    mgr1 = PaperPositionManager(ListAudit(), submit=approve)
    open_pos(mgr1, strat1)
    mgr1.update({"RELIANCE": ltf_frames([BENIGN])})       # processes the first closed bar
    save_state(path, positions=mgr1, strategies=[strat1])

    strat2 = FakeStrat()                                  # fresh process
    mgr2 = PaperPositionManager(ListAudit(), submit=approve)
    restore_all(load_state(path), positions=mgr2, strategies=[strat2])
    assert list(mgr2.open_positions()) == ["k1"]
    assert mgr2.open_notional() == pytest.approx(10 * 100.0)

    mgr2.update({"RELIANCE": ltf_frames([BENIGN, STOP_OUT])})   # only the new bar is processed
    assert strat2.exits == [("SL", 95.5)]


def test_restored_position_does_not_reprocess_old_bars(tmp_path):
    path = tmp_path / "state.json"
    strat1 = FakeStrat()
    mgr1 = PaperPositionManager(ListAudit(), submit=approve)
    open_pos(mgr1, strat1)
    mgr1.update({"RELIANCE": ltf_frames([BENIGN])})
    save_state(path, positions=mgr1, strategies=[strat1])

    strat2 = FakeStrat()
    mgr2 = PaperPositionManager(ListAudit(), submit=approve)
    restore_all(load_state(path), positions=mgr2, strategies=[strat2])
    mgr2.update({"RELIANCE": ltf_frames([BENIGN])})       # same bar again: nothing new
    assert strat2.exits == []
    assert mgr2.open_positions()["k1"].last_bar == f"{DAY} 09:15:00"


def test_pending_exit_survives_restart_and_is_sent_then(tmp_path):
    path = tmp_path / "state.json"
    strat1 = FakeStrat()
    rejecting = PaperPositionManager(ListAudit(), submit=lambda i: SimpleNamespace(sent=False, reasons=("x",)))
    open_pos(rejecting, strat1)
    rejecting.update({"RELIANCE": ltf_frames([STOP_OUT])})
    save_state(path, positions=rejecting, strategies=[strat1])

    strat2 = FakeStrat()
    mgr2 = PaperPositionManager(ListAudit(), submit=approve)
    restore_all(load_state(path), positions=mgr2, strategies=[strat2])
    assert mgr2.open_positions()["k1"].pending["reason"] == "SL"
    mgr2.update({"RELIANCE": ltf_frames([STOP_OUT])})
    assert strat2.exits == [("SL", 95.5)]


def test_restart_restores_strategy_state_and_handoff_marks(tmp_path):
    path = tmp_path / "state.json"
    strat1 = FakeStrat()
    strat1.level = 42.0
    handoff1 = Handoff()
    handoff1.mark_exhausted("RELIANCE", "core-key-1")
    save_state(path, positions=PaperPositionManager(ListAudit()), strategies=[strat1], handoff=handoff1)

    strat2 = FakeStrat()
    handoff2 = Handoff()
    restore_all(load_state(path), positions=PaperPositionManager(ListAudit()), strategies=[strat2],
                handoff=handoff2)
    assert strat2.level == 42.0
    assert handoff2.exhausted("RELIANCE", "core-key-1")
    assert not handoff2.exhausted("RELIANCE", "other")


def test_no_duplicate_position_for_same_key_after_restart(tmp_path):
    path = tmp_path / "state.json"
    strat = FakeStrat()
    mgr1 = PaperPositionManager(ListAudit(), submit=approve)
    open_pos(mgr1, strat)
    save_state(path, positions=mgr1, strategies=[strat])

    mgr2 = PaperPositionManager(ListAudit(), submit=approve)
    restore_all(load_state(path), positions=mgr2, strategies=[FakeStrat()])
    with pytest.raises(ValueError):
        open_pos(mgr2, FakeStrat())


def test_unknown_strategy_in_state_raises(tmp_path):
    path = tmp_path / "state.json"
    strat = FakeStrat()
    mgr1 = PaperPositionManager(ListAudit(), submit=approve)
    open_pos(mgr1, strat)
    save_state(path, positions=mgr1, strategies=[strat])

    other = FakeStrat()
    other.name = "engulfing_ocl"
    with pytest.raises(KeyError):
        restore_all(load_state(path), positions=PaperPositionManager(ListAudit()), strategies=[other])


def test_state_file_version_mismatch_is_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": %d, "positions": {}}' % (STATE_VERSION + 1), encoding="utf-8")
    with pytest.raises(ValueError):
        load_state(path)


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, positions=PaperPositionManager(ListAudit()), strategies=[FakeStrat()])
    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()
    assert load_state(tmp_path / "missing.json") is None


# ---------- runtime wiring: cycle saves, a new runtime restores ----------

def test_runtime_cycle_saves_state_and_new_runtime_resumes(tmp_path):
    from datetime import datetime
    from quant.core.calendar import TradingCalendar
    from quant.portfolio.tiers import TierTable
    from quant.strategies.paper_runtime import build_paper_runtime

    calendar = TradingCalendar(holidays=set(), verified=True)
    tiers = TierTable(enabled=False, bands=(), max_multiplier=2.0)
    state_path = tmp_path / "state.json"

    def make(strat, handoff=None):
        return build_paper_runtime(strategies={"RELIANCE": [strat]}, limits=LIMITS, tiers=tiers,
                                   calendar=calendar, capital=1_000_000.0, risk_fraction=0.0025,
                                   skip_dates=[], ledger_path=tmp_path / "ledger.json",
                                   kill_path=tmp_path / "KILL", state_path=state_path, handoff=handoff)

    strat1 = FakeStrat()
    rt1 = make(strat1)
    rt1.positions.open(key="k1", strategy=strat1, symbol="RELIANCE", qty=5, sig=sig(), bar_time=sig()["time"])
    rt1.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0},
        index=pd.date_range(f"{DAY} 09:15", periods=45, freq="1min"))}, {"RELIANCE": 5_000_000.0})
    assert state_path.exists()

    strat2 = FakeStrat()
    rt2 = make(strat2)                                    # new process, same state file
    assert list(rt2.positions.open_positions()) == ["k1"]
    assert rt2.positions.open_positions()["k1"].qty == 5
    assert any(r["kind"] == "state_restored" for r in rt2.audit.records)
