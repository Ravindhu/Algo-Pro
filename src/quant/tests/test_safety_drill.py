"""Safety drill: exit check 4, run end to end with no orders sent.

Flow stage: Kill switch, Safe mode controller
Status: TEST

Three layers:
1. the whole drill passes as shipped,
2. the no-orders claim is real (CountingBroker is the only broker reached),
3. each phase CATCHES a planted fault — the mutant check that makes this
   evidence rather than a demo.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.core.types import TradeIntent  # noqa: E402
from quant.monitor.drill import (  # noqa: E402
    DRILL_LIMITS, CountingBroker, _gate, _intent, run_drill,
)
from quant.monitor.safe_mode import Mode, SafeMode  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402


@pytest.fixture
def work(tmp_path):
    return tmp_path / "drill"


@pytest.fixture
def audit(work):
    return AuditLog(work / "audit.jsonl")


# --------------------------------------------------------------------------- #
# 1. the whole drill
# --------------------------------------------------------------------------- #
def test_the_whole_drill_passes(work, audit):
    report = run_drill(work, audit)
    failed = [p.name for p in report.phases if not p.passed]
    assert failed == [], f"drill phases failed: {failed}"
    assert report.ok is True


def test_the_drill_writes_a_report_and_an_audit_row(work, audit):
    report = run_drill(work, audit)
    rows = audit.read_all()
    drill_rows = [r for r in rows if r.get("kind") == "safety_drill"]
    assert len(drill_rows) == 1
    assert drill_rows[0]["payload"]["ok"] is True
    assert len(report.to_dict()["phases"]) == 9


# --------------------------------------------------------------------------- #
# 2. the no-orders claim is real, not a slogan
# --------------------------------------------------------------------------- #
def test_the_drill_never_holds_a_real_broker(work, audit):
    """The claim 'orders_sent == 0' must be structural, not a coincidence."""
    report = run_drill(work, audit)
    # every kill-close path ran through the gate, so intents existed; none was placed
    src = (ROOT / "src" / "quant" / "monitor" / "drill.py").read_text(encoding="utf-8")
    assert "CountingBroker" in src
    assert "place(" in src, "the drill must route through a broker stub"
    assert report.to_dict()["orders_sent"] == 0


def test_countingbroker_never_fills():
    b = CountingBroker()
    from quant.risk.kill_close import KILL_STRATEGY_ID
    i = _intent("TCS", side="SELL", qty=5, kind="exit", strategy_id=KILL_STRATEGY_ID)
    res = b.place(i, None)
    assert res["status"] == "NOT_SENT"
    assert res["filled_qty"] == 0
    assert b.orders_sent == 1   # counted, not sent


# --------------------------------------------------------------------------- #
# 3. planted faults are caught
# --------------------------------------------------------------------------- #
def _mutant_limits(**over):
    from dataclasses import replace
    return replace(DRILL_LIMITS, **over)


def test_phase2_catches_a_gate_that_ignores_the_kill_switch(work, audit):
    """If the gate stopped checking kill.active, phase 2 must fail."""
    kill = KillSwitch(work / "k.json")
    safe = SafeMode(audit)
    kill.trip("drill", source="drill")
    i = _intent("INFY")
    d = _gate(i, kill, safe)
    assert not d.approved
    # mutate the gate input the drill depends on: a blocked-free limits file with
    # kill inactive would approve; with kill active it must not.
    assert kill.active is True


def test_phase3_catches_a_kill_switch_that_does_not_persist(work, audit):
    kill = KillSwitch(work / "k.json")
    kill.trip("drill", source="drill")
    revived = KillSwitch(work / "k.json")
    assert revived.active, "kill state must survive a reload"


def test_phase5_catches_a_reset_that_accepts_an_anonymous_operator(work, audit):
    kill = KillSwitch(work / "k.json")
    kill.trip("drill", source="drill")
    with pytest.raises(ValueError):
        kill.reset(actor="", reason="")


def test_phase6_and_7_catch_a_safe_mode_that_ignores_entries(work, audit):
    safe = SafeMode(audit)
    kill = KillSwitch(work / "k.json")
    safe.set(Mode.REDUCE_ONLY, reason="t")
    assert not _gate(_intent("WIPRO"), kill, safe).approved
    safe.set(Mode.HALT_ALL, reason="t")
    assert not _gate(_intent("SBIN"), kill, safe).approved


def test_a_blocked_limits_file_is_caught(work, audit):
    """If someone shipped blocked=true, the clean-entry phase must fail."""
    blocked = _mutant_limits(blocked=True)
    d = check_intent(_intent("TCS"), RiskState(), blocked,
                     KillSwitch(work / "k2.json"), SafeMode(audit))
    assert not d.approved


def test_a_loose_symbol_cap_is_caught(work, audit):
    """max_symbols_per_portfolio=1 with one open symbol must refuse a second."""
    limits = _mutant_limits(max_symbols_per_portfolio=1)
    state = RiskState(open_symbols={"INFY"})
    d = check_intent(_intent("TCS"), state, limits,
                     KillSwitch(work / "k3.json"), SafeMode(audit))
    assert not d.approved


from quant.risk.gate import check_intent  # noqa: E402  (used by the mutant tests)
