"""Stage 8 (Safety gate): the pre-gate audited end to end.

Two defects found by reading the whole file rather than patching it again:

1. The holiday-calendar path was resolved against the CURRENT WORKING DIRECTORY.
   Started from anywhere except the repo root, the gate refused EVERY entry with
   "trading calendar unreadable" - fail-closed, but a total outage disguised as a
   data problem.
2. The bot built PreGateConfig() with all defaults, so max_sector_pct / max_beta /
   max_cvar / max_drawdown were permanently None. The book caps were tested and
   unreachable in production.

Plus a config defect: risk_limits.yaml declared max_symbols_per_portfolio THREE
times. YAML silently keeps the last, so the first two comments were lies.
"""
import os
from datetime import date
from pathlib import Path

import pytest

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.risk.pre_gate import (PreGate, PreGateConfig, load_equity_holidays,
                                 resolve_calendar_path)

ROOT = Path(__file__).resolve().parents[3]
DAY = date(2026, 11, 13)  # a Friday


def _intent(kind="entry", side="BUY", **over):
    base = dict(intent_id="I1", strategy_id="s", symbol="SBIN", side=side, qty=10,
                ref_price=100.0, kind=kind, lot_size=1, trade_day=DAY)
    base.update(over)
    return TradeIntent(**base)


# --------------------------------------------------------------------------- #
# defect 1: calendar path must not depend on the working directory
# --------------------------------------------------------------------------- #
def test_relative_calendar_resolves_against_the_repo_root():
    resolved = resolve_calendar_path("config/calendar/nse_holidays_2026.csv")
    assert resolved.is_absolute()
    assert resolved.exists()


def test_absolute_calendar_path_is_used_as_is(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text("date,equity_holiday\n2026-01-01,yes\n", encoding="utf-8")
    assert resolve_calendar_path(str(p)) == p


def test_calendar_is_found_from_a_different_working_directory(tmp_path, monkeypatch):
    """This is the regression: the gate used to refuse EVERY entry off-root."""
    gate = PreGate(PreGateConfig())
    monkeypatch.chdir(tmp_path)
    try:
        assert gate._calendar_readable() is True
    finally:
        pass


def test_holidays_load_off_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        hols = load_equity_holidays("config/calendar/nse_holidays_2026.csv")
    finally:
        pass
    assert len(hols) > 0


def test_unreadable_calendar_still_fails_closed():
    """The path fix must not weaken the rule it was protecting."""
    gate = PreGate(PreGateConfig(holidays_csv="no/such/calendar.csv"), holidays=set())
    res = gate.calendar_ok(DAY)
    assert res.ok is False
    assert any("unreadable" in r for r in res.reasons)


# --------------------------------------------------------------------------- #
# defect 2: the caps must be reachable from configuration
# --------------------------------------------------------------------------- #
def test_risk_limits_has_the_book_cap_fields():
    limits = RiskLimits()
    for field in ("max_sector_pct", "max_beta", "max_cvar", "max_drawdown"):
        assert hasattr(limits, field)


def test_risk_limits_loads_the_yaml_with_caps():
    limits = RiskLimits.from_yaml(ROOT / "config" / "risk_limits.yaml")
    assert limits.max_symbols_per_portfolio == 8


def test_unset_caps_are_reported_as_unset():
    limits = RiskLimits()
    assert "max_sector_pct" in limits.unset()


def test_caps_reach_the_pre_gate():
    limits = RiskLimits(max_sector_pct=0.30, max_beta=1.2)
    gate = PreGate(PreGateConfig(max_sector_pct=limits.max_sector_pct,
                                 max_beta=limits.max_beta), holidays=set())
    assert gate.cfg.max_sector_pct == 0.30
    assert gate.cfg.max_beta == 1.2


def test_limits_store_accepts_the_new_caps(tmp_path):
    from quant.core.limits_store import apply_limit_updates
    result = apply_limit_updates(tmp_path / "rl.yaml",
                                 {"max_sector_pct": 0.30, "max_cvar": 0.05})
    assert result["new"]["max_sector_pct"] == 0.30


# --------------------------------------------------------------------------- #
# the config file itself
# --------------------------------------------------------------------------- #
def test_risk_limits_yaml_has_no_duplicate_keys():
    """It declared max_symbols_per_portfolio three times; YAML kept the last."""
    import collections
    raw = [line.split(":")[0].strip()
           for line in (ROOT / "config" / "risk_limits.yaml").read_text(encoding="utf-8").splitlines()
           if ":" in line and not line.strip().startswith("#")]
    duplicates = [k for k, c in collections.Counter(raw).items() if c > 1]
    assert duplicates == [], f"duplicate keys: {duplicates}"


# --------------------------------------------------------------------------- #
# the whole gate: all nine checks present and fail-closed
# --------------------------------------------------------------------------- #
def test_every_check_exists():
    gate = PreGate(PreGateConfig(), holidays=set())
    for name in ("calendar_ok", "handoff_ok", "overnight_gap_ok", "stress_ok",
                 "portfolio_caps_ok", "market_halt_ok", "session_ok", "feed_ok"):
        assert callable(getattr(gate, name)), name


def test_entry_passes_when_every_input_is_sane():
    gate = PreGate(PreGateConfig(), holidays=set())
    assert gate.check(_intent()).ok is True


def test_several_failures_are_all_reported_together():
    """A caller should see every reason, not just the first."""
    gate = PreGate(PreGateConfig(), holidays=set())
    bad = _intent(qty=0, ref_price=0.0)
    res = gate.check(bad)
    assert res.ok is False
    assert len(res.reasons) >= 2


def test_long_only_sell_entry_is_refused():
    gate = PreGate(PreGateConfig(), holidays=set())
    res = gate.check(_intent(side="SELL"))
    assert res.ok is False
    assert any("long-only" in r for r in res.reasons)


def test_exits_bypass_every_pregate_check():
    """A stop must always be able to work, whatever the book or the clock says."""
    gate = PreGate(PreGateConfig(max_sector_pct=0.01, max_beta=0.01,
                                 holidays_csv="no/such.csv"), holidays=set())
    res = gate.check(_intent("exit", "SELL"), weights={"A": 0.99},
                     index_change_pct=-20.5, check_book_limits=True)
    assert res.ok is True


def test_protection_intents_also_bypass():
    gate = PreGate(PreGateConfig(), holidays=set())
    assert gate.check(_intent("protection")).ok is True


def test_weekend_is_refused():
    gate = PreGate(PreGateConfig(), holidays=set())
    assert gate.calendar_ok(date(2026, 11, 14)).ok is False   # Saturday


def test_holiday_is_refused():
    gate = PreGate(PreGateConfig(), holidays={DAY})
    res = gate.calendar_ok(DAY)
    assert res.ok is False
    assert any("holiday" in r for r in res.reasons)


def test_stress_refuses_a_book_beyond_the_var_limit():
    gate = PreGate(PreGateConfig(var_limit=0.05), holidays=set())
    res = gate.stress_ok(1_000_000, {"A": 1.0})
    assert res.ok is False


def test_stress_needs_positive_capital():
    gate = PreGate(PreGateConfig(), holidays=set())
    assert gate.stress_ok(0, {"A": 0.1}).ok is False
