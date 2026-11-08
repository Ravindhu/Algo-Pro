"""HALT_ALL policy (owner decision 2026-11-08): risk-reducing SELL exits pass; entries and BUY exits do not.

Hand-checked. Uses the real gate, the real risk_limits.yaml for the paper end-to-end case, and the
paper position manager.
"""
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.strategies.positions import PaperPositionManager
from quant.strategies.protect import Protection
from quant.strategies.scanner import ListAudit

ROOT = Path(__file__).resolve().parents[2]
LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk_limits.yaml")
DAY = "2026-11-09"


def intent(**kw):
    base = dict(intent_id="I1", strategy_id="ocl", symbol="RELIANCE", side="SELL", qty=10,
                ref_price=95.5, kind="exit", lot_size=1, adv_shares=5e6)
    base.update(kw)
    return TradeIntent(**base)


def halted(tmp_path):
    s = SafeMode()
    s.set(Mode.HALT_ALL, "feed error (test)")
    return s, KillSwitch(tmp_path / "KILL")


def decide(i, safe, kill, limits=LIMITS):
    return check_intent(i, RiskState(open_notional=0.0, open_symbols=set()), limits, kill, safe,
                        available_funds=1_000_000.0)


def test_sell_exit_passes_halt_all(tmp_path):
    safe, kill = halted(tmp_path)
    d = decide(intent(), safe, kill)
    assert d.approved, d.reasons


def test_entry_is_still_blocked_under_halt_all(tmp_path):
    safe, kill = halted(tmp_path)
    d = decide(intent(kind="entry", side="BUY"), safe, kill)
    assert not d.approved and "safe mode: halt-all" in d.reasons


def test_buy_exit_is_still_blocked_under_halt_all(tmp_path):
    safe, kill = halted(tmp_path)
    d = decide(intent(side="BUY"), safe, kill)
    assert not d.approved and "safe mode: halt-all" in d.reasons


def test_risk_limits_blocked_still_stops_a_sell_exit(tmp_path):
    safe, kill = halted(tmp_path)
    limits = RiskLimits(**{**LIMITS.__dict__, "blocked": True})
    d = decide(intent(), safe, kill, limits=limits)
    assert not d.approved and "risk_limits.blocked is set" in d.reasons


def test_duplicate_exit_id_is_still_refused(tmp_path):
    safe, kill = halted(tmp_path)
    d = check_intent(intent(), RiskState(seen_ids={"I1"}), LIMITS, kill, safe, available_funds=1e6)
    assert not d.approved and "duplicate intent id" in d.reasons


def test_paper_stop_out_fills_while_halted(tmp_path):
    """End to end: a stop-out during HALT_ALL is sent through the real gate and closes the position."""
    safe, kill = halted(tmp_path)
    strat = SimpleNamespace(name="ocl", symbol="RELIANCE", htf_tf="15minute", ltf_tf="5minute",
                            p=SimpleNamespace(protection=Protection(atr_trail_enabled=False), atr_period=14),
                            exits=[], exit_flag=False)
    strat.exit_check = lambda frames: strat.exit_flag
    strat.on_exit = lambda reason, price: strat.exits.append((reason, price))

    def submit(i):
        d = decide(i, safe, kill)
        return SimpleNamespace(sent=d.approved, reasons=d.reasons)

    mgr = PaperPositionManager(ListAudit(), submit=submit)
    mgr.open(key="k1", strategy=strat, symbol="RELIANCE", qty=10, bar_time=f"{DAY} 09:10:00",
             sig={"entry_ref": 100.0, "trigger": 97.0, "htf_sl": 96.0, "sl_price": 94.0,
                  "tp_dist": None, "exit_mode": "DYNAMIC", "time": f"{DAY} 09:10:00"})
    idx = pd.date_range(f"{DAY} 09:15", periods=2, freq="5min")
    rows = [(100, 100.5, 99.5, 95.5), (95.5, 95.5, 95.5, 95.5)]   # first bar closes below floor 96
    df = pd.DataFrame({"open": [r[0] for r in rows], "high": [r[1] for r in rows],
                       "low": [r[2] for r in rows], "close": [r[3] for r in rows], "volume": 1000.0}, index=idx)
    df = pd.concat([df, pd.DataFrame({"open": [95.5], "high": [95.5], "low": [95.5], "close": [95.5],
                                      "volume": [1000.0]}, index=[idx[-1] + pd.Timedelta(minutes=5)])])
    frames = {"5minute": df, "15minute": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                                        "volume": 1.0},
                                                       index=pd.date_range(f"{DAY} 09:15", periods=1, freq="15min"))}
    mgr.update({"RELIANCE": frames})
    assert strat.exits == [("SL", 95.5)]
    assert "k1" not in mgr.open_positions()
