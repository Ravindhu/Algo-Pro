"""Startup broker reconcile for the BUY book (read-only). Hand-checked, fabricated broker bodies.

Covers: body parsing, match / mismatch / unavailable, entries blocked but exits still run, restore before
reconcile, live start with no reader refused, and the read path never calls a place method.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.core.calendar import TradingCalendar
from quant.core.config import RiskLimits
from quant.execution.readonly_client import OpenAlgoReadOnly
from quant.monitor.broker_recon import (PositionsView, make_positionbook_reader, parse_positionbook,
                                        startup_reconcile)
from quant.portfolio.tiers import TierTable
from quant.strategies.paper_runtime import build_paper_runtime
from quant.strategies.positions import PaperPositionManager
from quant.strategies.protect import Protection
from quant.strategies.scanner import ListAudit

ROOT = Path(__file__).resolve().parents[2]
LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk_limits.yaml")
CALENDAR = TradingCalendar(holidays=set(), verified=True)
TIERS = TierTable(enabled=False, bands=(), max_multiplier=2.0)
DAY = "2026-11-09"


class FakeStrat:
    name = "ocl"
    symbol = "RELIANCE"
    htf_tf = "15minute"
    ltf_tf = "5minute"

    def __init__(self):
        self.p = SimpleNamespace(protection=Protection(atr_trail_enabled=False), atr_period=14)
        self.signal_calls = 0
        self.exits = []

    def exit_check(self, frames):
        return False

    def on_exit(self, reason, price):
        self.exits.append((reason, price))

    def on_entry(self):
        pass

    def signal_frames(self, frames):
        self.signal_calls += 1
        return None

    def state(self):
        return {}

    def restore(self, d):
        pass


def sig(time=f"{DAY} 09:10:00"):
    return {"entry_ref": 100.0, "trigger": 97.0, "htf_sl": 96.0, "sl_price": 94.0,
            "tp_dist": None, "exit_mode": "DYNAMIC", "time": time}


def bars(n=45):
    idx = pd.date_range(f"{DAY} 09:15", periods=n, freq="1min")
    return pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0},
                        index=idx)


def body(rows, status="success"):
    return {"status": status, "data": rows}


def runtime(tmp_path, strat, broker=None, require=False, state=None):
    return build_paper_runtime(strategies={"RELIANCE": [strat]}, limits=LIMITS, tiers=TIERS,
                               calendar=CALENDAR, capital=1_000_000.0, risk_fraction=0.0025,
                               skip_dates=[], ledger_path=tmp_path / "ledger.json",
                               kill_path=tmp_path / "KILL", state_path=state,
                               broker_positions=broker, require_broker=require)


def open_one(rt, strat, qty=10):
    rt.positions.open(key="k1", strategy=strat, symbol="RELIANCE", qty=qty, sig=sig(), bar_time=sig()["time"])


# ---------- body parsing ----------

def test_parse_nets_per_symbol_and_filters_exchange():
    rows = [{"symbol": "RELIANCE", "exchange": "NSE", "netqty": "10"},
            {"symbol": "RELIANCE", "exchange": "NSE", "netqty": "5"},
            {"symbol": "TCS", "exchange": "BSE", "netqty": "7"}]
    assert parse_positionbook(body(rows)) == {"RELIANCE": 15}


def test_parse_keeps_broker_shorts_as_negative():
    assert parse_positionbook(body([{"symbol": "INFY", "exchange": "NSE", "netqty": "-3"}])) == {"INFY": -3}


@pytest.mark.parametrize("bad", [
    {"status": "error", "data": []},
    {"status": "success", "data": "not a list"},
    {"status": "success", "data": [{"exchange": "NSE", "netqty": "1"}]},
    "garbage",
])
def test_unexpected_body_raises(bad):
    with pytest.raises(ValueError):
        parse_positionbook(bad)


# ---------- reconcile result ----------

def test_match_allows_entries(tmp_path):
    strat = FakeStrat()
    audit = ListAudit()
    mgr = PaperPositionManager(audit)
    mgr.open(key="k1", strategy=strat, symbol="RELIANCE", qty=10, sig=sig(), bar_time=sig()["time"])
    gate = startup_reconcile(mgr, lambda: {"RELIANCE": 10}, audit=audit)
    assert gate.status == "match" and gate.entries_allowed
    assert audit.records[-1]["kind"] == "recon" and audit.records[-1]["status"] == "match"


def test_mismatch_blocks_entries_and_reports_both_sides():
    mgr = PaperPositionManager(ListAudit())
    mgr.open(key="k1", strategy=FakeStrat(), symbol="RELIANCE", qty=10, sig=sig(), bar_time=sig()["time"])
    gate = startup_reconcile(mgr, lambda: {"RELIANCE": 15, "TCS": 4})
    assert gate.status == "mismatch" and not gate.entries_allowed
    assert gate.mismatches == {"RELIANCE": [10, 15], "TCS": [0, 4]}


def test_broker_short_is_a_mismatch_for_long_only_book():
    gate = startup_reconcile(PaperPositionManager(ListAudit()), lambda: {"INFY": -3})
    assert gate.status == "mismatch" and not gate.entries_allowed


def test_failed_read_is_unavailable_and_blocks(tmp_path):
    def broken():
        raise ConnectionError("broker down")
    gate = startup_reconcile(PaperPositionManager(ListAudit()), broken)
    assert gate.status == "unavailable" and not gate.entries_allowed
    assert "broker down" in gate.error


def test_paper_without_broker_is_skipped_and_allowed():
    gate = startup_reconcile(PaperPositionManager(ListAudit()), None)
    assert gate.status == "skipped" and gate.entries_allowed


def test_live_without_broker_reader_is_refused():
    with pytest.raises(ValueError):
        startup_reconcile(PaperPositionManager(ListAudit()), None, require_broker=True)


# ---------- wiring into the paper runtime ----------

def test_mismatch_blocks_new_entries_but_exits_still_run(tmp_path):
    strat = FakeStrat()
    rt = runtime(tmp_path, strat, broker=lambda: {"RELIANCE": 99})
    assert not rt.recon.entries_allowed
    rt.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": bars()}, {"RELIANCE": 5_000_000.0})
    assert strat.signal_calls == 0                      # scanner not run while recon fails
    assert any(r["kind"] == "entries_blocked" for r in rt.audit.records)


def test_match_lets_the_scanner_run(tmp_path):
    strat = FakeStrat()
    rt = runtime(tmp_path, strat, broker=lambda: {})
    assert rt.recon.status == "match"
    rt.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": bars()}, {"RELIANCE": 5_000_000.0})
    assert strat.signal_calls == 1


def test_exit_still_sent_while_recon_blocks_entries(tmp_path):
    """A stop-out must go out even when broker and bot disagree; only new entries are blocked."""
    state = tmp_path / "state.json"
    strat1 = FakeStrat()
    rt1 = runtime(tmp_path, strat1, broker=lambda: {"RELIANCE": 10}, state=state)
    open_one(rt1, strat1)
    rt1.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": bars()}, {"RELIANCE": 5_000_000.0})
    assert state.exists()

    strat2 = FakeStrat()
    rt2 = runtime(tmp_path, strat2, broker=lambda: {"RELIANCE": 99}, state=state)
    assert not rt2.recon.entries_allowed
    # Restored last processed bar is 09:50. The crash starts at 09:55 (minute index 40), after it.
    crash = bars(56)
    crash.loc[crash.index[40]:, ["open", "high", "low", "close"]] = 90.0
    rt2.cycle(datetime(2026, 11, 9, 10, 10), {"RELIANCE": crash}, {"RELIANCE": 5_000_000.0})
    assert strat2.exits and strat2.exits[0][0] in ("SL", "DISASTER", "HTF")


def test_restore_happens_before_reconcile(tmp_path):
    """The restored open position counts in the comparison, so a matching broker gives a match."""
    state = tmp_path / "state.json"
    strat1 = FakeStrat()
    rt1 = runtime(tmp_path, strat1, state=state)
    open_one(rt1, strat1, qty=10)
    rt1.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": bars()}, {"RELIANCE": 5_000_000.0})

    rt2 = runtime(tmp_path, FakeStrat(), broker=lambda: {"RELIANCE": 10}, state=state)
    assert rt2.recon.status == "match"


def test_live_runtime_requires_a_broker_reader(tmp_path):
    with pytest.raises(ValueError):
        runtime(tmp_path, FakeStrat(), broker=None, require=True)


# ---------- read-only path ----------

def test_reader_uses_only_the_positionbook_path():
    calls = []

    def transport(url, payload):
        calls.append((url, payload))
        return body([{"symbol": "RELIANCE", "exchange": "NSE", "netqty": "10"}])

    readonly = OpenAlgoReadOnly("http://openalgo.local", transport)
    fetch = make_positionbook_reader(readonly, "KEY")
    assert fetch() == {"RELIANCE": 10}
    assert calls == [("http://openalgo.local/api/v1/positionbook", {"apikey": "KEY"})]
    assert not any(hasattr(readonly, name) for name in ("place", "cancel", "modify"))


def test_reader_refuses_an_empty_key():
    readonly = OpenAlgoReadOnly("http://openalgo.local", lambda u, p: body([]))
    with pytest.raises(ValueError):
        make_positionbook_reader(readonly, "")()
