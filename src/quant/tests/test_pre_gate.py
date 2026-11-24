"""Pre-gate safety checks: calendar, blackout, gap, handoff contract, stress/VaR."""
from datetime import date
from pathlib import Path

import pytest

from quant.core.types import TradeIntent
from quant.risk.pre_gate import PreGate, PreGateConfig, load_equity_holidays

CSV = "date,equity_holiday\n2026-03-04,yes\n2026-11-11,no\n"


def _write_csv(tmp_path: Path) -> Path:
    p = tmp_path / "hol.csv"
    p.write_text(CSV, encoding="utf-8")
    return p


def _intent(**over):
    base = dict(intent_id="i1", strategy_id="s", symbol="INFY", side="BUY", qty=10,
                ref_price=100.0, kind="entry", prev_close=None, trade_day=None)
    base.update(over)
    return TradeIntent(**base)


def test_load_equity_holidays(tmp_path):
    days = load_equity_holidays(_write_csv(tmp_path))
    assert date(2026, 3, 4) in days and date(2026, 11, 11) not in days


def test_weekend_and_holiday_block(tmp_path):
    pg = PreGate(PreGateConfig(holidays_csv=str(_write_csv(tmp_path))))  # lazy-load the CSV
    r = pg.calendar_ok(date(2026, 3, 4))
    assert not r.ok
    assert any("holiday" in x for x in r.reasons)
    r2 = pg.calendar_ok(date(2026, 11, 14))  # Saturday
    assert not r2.ok and any("weekend" in x for x in r2.reasons)


def test_calendar_unreadable_fails_closed(tmp_path):
    pg = PreGate(PreGateConfig(holidays_csv=str(tmp_path / "missing.csv")), holidays=set())
    r = pg.calendar_ok(date(2026, 11, 11))
    assert not r.ok
    assert any("unreadable" in x for x in r.reasons)


def test_event_blackout_blocks(tmp_path):
    pg = PreGate(PreGateConfig(holidays_csv=str(_write_csv(tmp_path))), holidays=set())
    r = pg.calendar_ok(date(2026, 2, 1), blackout_days=[date(2026, 2, 1)])
    assert not r.ok and any("blackout" in x for x in r.reasons)


def test_overnight_gap_entry_refused():
    pg = PreGate()
    ok = pg.overnight_gap_ok(_intent(ref_price=105.0, prev_close=100.0))
    assert ok.ok
    bad = pg.overnight_gap_ok(_intent(ref_price=111.0, prev_close=100.0))
    assert not bad.ok and any("gap" in x for x in bad.reasons)


def test_handoff_contract_cases():
    pg = PreGate()
    assert pg.handoff_ok(_intent()).ok
    assert not pg.handoff_ok(_intent(qty=0)).ok
    assert not pg.handoff_ok(_intent(ref_price=-1)).ok
    sell_entry = pg.handoff_ok(_intent(side="SELL"))
    assert not sell_entry.ok and any("long-only" in x for x in sell_entry.reasons)
    sell_exit = pg.handoff_ok(_intent(side="SELL", kind="exit"))
    assert sell_exit.ok


def test_stress_var_blocks():
    pg = PreGate(PreGateConfig(var_limit=0.05))
    ok = pg.stress_ok(100_000.0, {"A": 0.04, "B": 0.04})
    assert ok.ok
    bad = pg.stress_ok(100_000.0, {"A": 0.6})
    assert not bad.ok
    nocap = pg.stress_ok(0.0, {"A": 0.1})
    assert not nocap.ok  # fail closed: no capital known


def test_exits_bypass_pregate(tmp_path):
    pg = PreGate(PreGateConfig(holidays_csv=str(tmp_path / "missing.csv")))
    assert pg.check(_intent(kind="exit", side="SELL")).ok


def test_combined_check_entry_pass_and_fail(tmp_path):
    pg = PreGate(PreGateConfig(holidays_csv=str(_write_csv(tmp_path))))
    good = pg.check(_intent(trade_day=date(2026, 11, 11)), capital=100_000.0, weights={"INFY": 0.05})
    assert good.ok
    hol = pg.check(_intent(trade_day=date(2026, 3, 4)))
    assert not hol.ok


def test_screen_rows_and_write_file(tmp_path):
    """Screener output file: only safe rows are written, atomically."""
    from quant.data.screener_engine import screen_rows, write_screen_file

    payload = {"candidates": [
        {"symbol": "INFY", "selected": True, "circuit_pct": 0.2, "asm_gsm": False},
        {"symbol": "BAD1", "circuit_pct": None, "asm_gsm": False},        # no circuit -> dropped
        {"symbol": "BAD2", "circuit_pct": 0.1, "asm_gsm": True},          # ASM -> dropped
        {"symbol": "BAD3", "circuit_pct": 5.0, "asm_gsm": False},         # implausible -> dropped
    ]}
    doc = screen_rows(payload, as_of="2026-11-11T10:00:00")
    assert [r["symbol"] for r in doc["results"]] == ["INFY"]
    assert doc["results"][0] == {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}

    out = tmp_path / "screened" / "latest.json"
    n = write_screen_file(payload, out, as_of=doc["as_of"])
    assert n == 1
    import json
    assert json.loads(out.read_text(encoding="utf-8"))["results"][0]["symbol"] == "INFY"
