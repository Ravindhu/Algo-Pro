"""Tests for the paper 1-minute scanner and the max-quantity rule (2026-11-08).

Uses the real risk gate (check_intent) and the real config/risk_limits.yaml. Nothing is sent to a broker.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from quant.core.calendar import TradingCalendar
from quant.core.config import RiskLimits
from quant.core.types import GateDecision
from quant.execution.pov_executor import PovExecutor
from quant.monitor.safe_mode import SafeMode
from quant.portfolio.paper_qty import max_entry_qty
from quant.portfolio.tiers import TierTable
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.strategies.scanner import PAPER_KEY, ListAudit, PaperSender, Scanner, SignalLedger

ROOT = Path(__file__).resolve().parents[2]
LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk_limits.yaml")
# Test copy only. The real risk_limits.yaml leaves max_symbols_per_portfolio null (owner decision needed).
TEST_LIMITS = dataclasses.replace(LIMITS, max_symbols_per_portfolio=5)
ADV = {"RELIANCE": 5_000_000.0}
TIERS_OFF = TierTable(enabled=False, bands=(), max_multiplier=2.0)
CALENDAR = TradingCalendar(holidays=set(), verified=True)
MON = datetime(2026, 11, 9, 10, 0)                 # Monday, inside the session
SUN_MUHURAT = datetime(2026, 11, 8, 18, 0)         # Sunday 2026-11-08 (owner: skip this date)


def frames(volume_last_closed: float = 1000.0) -> dict:
    idx5 = pd.date_range("2026-11-09 09:15", periods=6, freq="5min")
    df5 = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                        "volume": [1000.0] * 4 + [volume_last_closed, 500.0]}, index=idx5)
    idx15 = pd.date_range("2026-11-09 09:15", periods=3, freq="15min")
    df15 = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                         "volume": 1000.0}, index=idx15)
    return {"5minute": df5, "15minute": df15}


class FakeStrat:
    name = "ocl"
    htf_tf = "15minute"
    ltf_tf = "5minute"

    def __init__(self, sig):
        self.sig = sig
        self.entered = 0

    def signal_frames(self, frames):
        return self.sig

    def on_entry(self):
        self.entered += 1


def signal(entry=100.0, stop=98.0, bar="2026-11-09 09:40:00"):
    return {"side": "BUY", "entry_ref": entry, "sl_price": stop, "exit_mode": "FIXED", "time": bar}


def make_scanner(tmp_path, sig, limits=TEST_LIMITS, audit=None, ledger=None, skip=(), sender=None, adv=ADV):
    audit = audit or ListAudit()
    sender = sender or PaperSender()
    kill = KillSwitch(tmp_path / "KILL")
    safe = SafeMode()

    def gate(intent):
        return check_intent(intent, RiskState(), limits, kill, safe, available_funds=None)

    executor = PovExecutor(gate=gate, sender=sender, api_key=PAPER_KEY, audit=audit, pov=0.1)
    strat = FakeStrat(sig)
    scanner = Scanner(strategies={"RELIANCE": [strat]}, executor=executor,
                      ledger=ledger or SignalLedger(tmp_path / "ledger.json"), audit=audit,
                      capital=100_000.0, risk_fraction=0.0025, limits=limits, tiers=TIERS_OFF,
                      calendar=CALENDAR, skip_dates=skip, adv_shares=adv)
    return scanner, strat, sender, audit


# ---------- quantity rule ----------

def test_qty_is_lowest_of_risk_budget_and_caps():
    # risk budget: 100000 * 0.0025 / 2 = 125 shares; max_order_quantity 10 binds
    d = max_entry_qty(entry=100.0, stop=98.0, capital=100_000, risk_fraction=0.0025, limits=LIMITS,
                      open_notional=0.0, tiers=TIERS_OFF)
    assert d.base_qty == 125
    assert d.qty == 10
    assert d.binding == "max_order_quantity"


def test_risk_budget_binds_when_caps_are_loose():
    loose = RiskLimits(max_order_quantity=1000, max_order_notional=1e9, max_open_exposure=1e9)
    d = max_entry_qty(entry=100.0, stop=98.0, capital=100_000, risk_fraction=0.0025, limits=loose,
                      open_notional=0.0, tiers=TIERS_OFF)
    assert d.qty == 125
    assert d.binding == "none"                 # no cap was lower than the risk budget


def test_remaining_exposure_limits_qty():
    # open exposure 19,500 of 20,000 leaves 500 INR = 5 shares at 100
    d = max_entry_qty(entry=100.0, stop=98.0, capital=100_000, risk_fraction=0.0025, limits=LIMITS,
                      open_notional=19_500.0, tiers=TIERS_OFF)
    assert d.qty == 5


def test_missing_limit_fails_closed():
    no_qty_cap = RiskLimits(max_order_notional=10_000, max_open_exposure=20_000)
    d = max_entry_qty(entry=100.0, stop=98.0, capital=100_000, risk_fraction=0.0025, limits=no_qty_cap,
                      open_notional=0.0, tiers=TIERS_OFF)
    assert d.qty == 0 and d.binding == "limit_missing"


def test_stop_above_entry_is_rejected():
    d = max_entry_qty(entry=100.0, stop=101.0, capital=100_000, risk_fraction=0.0025, limits=LIMITS,
                      open_notional=0.0, tiers=TIERS_OFF)
    assert d.qty == 0 and d.binding == "invalid_stop"


def test_tiers_enabled_without_conviction_raises():
    tiers_on = TierTable(enabled=True, bands=((0.5, 1.0),), max_multiplier=2.0)
    with pytest.raises(ValueError):
        max_entry_qty(entry=100.0, stop=98.0, capital=100_000, risk_fraction=0.0025, limits=LIMITS,
                      open_notional=0.0, tiers=tiers_on)


# ---------- scanner ----------

def test_signal_becomes_paper_fill_through_gate_and_slicer(tmp_path):
    scanner, strat, sender, audit = make_scanner(tmp_path, signal())
    scanner.cycle(MON, {"RELIANCE": frames()})
    assert len(sender.fills) == 1
    fill = sender.fills[0]
    assert fill["qty"] == 10 and fill["mode"] == "PAPER" and fill["symbol"] == "RELIANCE"
    assert strat.entered == 1
    kinds = [r["kind"] for r in audit.records]
    assert "signal" in kinds and "qty" in kinds and "gate" in kinds and "child_sent" in kinds


def test_same_signal_bar_is_never_traded_twice(tmp_path):
    scanner, _, sender, _ = make_scanner(tmp_path, signal())
    scanner.cycle(MON, {"RELIANCE": frames()})
    scanner.cycle(MON, {"RELIANCE": frames()})
    assert len(sender.fills) == 1


def test_restart_does_not_retrade_a_handled_bar(tmp_path):
    scanner, _, sender, _ = make_scanner(tmp_path, signal())
    scanner.cycle(MON, {"RELIANCE": frames()})
    scanner2, _, sender2, _ = make_scanner(tmp_path, signal())   # new process, same ledger file
    scanner2.cycle(MON, {"RELIANCE": frames()})
    assert len(sender.fills) == 1 and len(sender2.fills) == 0


def test_muhurat_skip_date_sends_nothing(tmp_path):
    scanner, strat, sender, audit = make_scanner(tmp_path, signal(), skip=[SUN_MUHURAT.date()])
    scanner.cycle(SUN_MUHURAT, {"RELIANCE": frames()})
    assert sender.fills == [] and strat.entered == 0
    assert audit.records[-1]["kind"] == "cycle_skipped"


def test_closed_session_sends_nothing(tmp_path):
    scanner, _, sender, audit = make_scanner(tmp_path, signal())
    scanner.cycle(datetime(2026, 11, 9, 8, 0), {"RELIANCE": frames()})   # before 09:15
    assert sender.fills == []
    assert audit.records[-1]["kind"] == "cycle_skipped"


def test_zero_volume_bar_sends_nothing_and_is_not_retried(tmp_path):
    scanner, _, sender, audit = make_scanner(tmp_path, signal())
    scanner.cycle(MON, {"RELIANCE": frames(volume_last_closed=0.0)})
    assert sender.fills == []
    assert any(r["kind"] == "slice_stopped" for r in audit.records)
    scanner.cycle(MON, {"RELIANCE": frames(volume_last_closed=5000.0)})   # same signal bar, now with volume
    assert sender.fills == []                                              # ledger already holds the bar


def test_gate_rejection_is_audited_and_nothing_is_sent(tmp_path):
    blocked = dataclasses.replace(TEST_LIMITS, blocked=True)
    scanner, strat, sender, audit = make_scanner(tmp_path, signal(), limits=blocked)
    scanner.cycle(MON, {"RELIANCE": frames()})
    assert sender.fills == [] and strat.entered == 0
    assert any(r["kind"] == "gate" and r["approved"] is False for r in audit.records)


def test_qty_zero_is_skipped_with_reason(tmp_path):
    scanner, _, sender, audit = make_scanner(tmp_path, signal(entry=100.0, stop=98.0))
    scanner.capital = 0.1                      # risk budget 0.1 * 0.0025 / 2 rounds to 0 shares
    scanner.cycle(MON, {"RELIANCE": frames()})
    assert sender.fills == []
    assert any(r["kind"] == "skipped" for r in audit.records)


def test_real_limits_file_has_owner_symbol_limit():
    """Owner decision 2026-11-08: max_symbols_per_portfolio = 8 in config/risk_limits.yaml."""
    assert LIMITS.max_symbols_per_portfolio == 8
    scanner, _, sender, audit = make_scanner(tmp_path_factory_dummy(), signal(), limits=LIMITS)
    scanner.cycle(MON, {"RELIANCE": frames()})
    gate_rec = [r for r in audit.records if r["kind"] == "gate"][0]
    assert not any("max_symbols_per_portfolio" in reason for reason in gate_rec["reasons"])


def tmp_path_factory_dummy():
    import tempfile
    return Path(tempfile.mkdtemp())


def test_symbol_without_adv_is_rejected_by_gate(tmp_path):
    scanner, _, sender, audit = make_scanner(tmp_path, signal(), adv={})
    scanner.cycle(MON, {"RELIANCE": frames()})
    assert sender.fills == []
    gate_rec = [r for r in audit.records if r["kind"] == "gate"][0]
    assert any("ADV unknown" in reason for reason in gate_rec["reasons"])
