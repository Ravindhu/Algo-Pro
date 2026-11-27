"""Phase 2 live blockers (2026-11-27), part 1: sandbox band, preflight probe, margin direction.

Covers audit items 10 and 13 (sandbox 20% band reaching live), 12 and 18 (preflight probing an
index), and 19 (margin_for_order accepts any action).
"""
import inspect
from datetime import datetime

import pytest

from quant.data.screener_engine import screen_rows
from quant.execution.readonly_client import OpenAlgoReadOnly
from quant.execution.strategy_bot import read_screen

NOW = datetime(2026, 11, 27, 10, 0)


def _write_screen(tmp_path, rows):
    import json
    path = tmp_path / "latest.json"
    path.write_text(json.dumps({"as_of": "2026-11-27T10:00:00", "results": rows}), encoding="utf-8")
    return path


def _row(symbol, band_source=None):
    row = {"symbol": symbol, "selected": True, "asm_gsm": False, "circuit_pct": 0.2}
    if band_source is not None:
        row["band_source"] = band_source
    return row


# ---- item 10 / 13: sandbox band must never reach the live bot ------------------------------

def test_live_reader_refuses_sandbox_fallback_band(tmp_path):
    path = _write_screen(tmp_path, [_row("INFY", "broker"), _row("TCS", "sandbox_default")])
    names = [r["symbol"] for r in read_screen(path, NOW, 10)]
    assert names == ["INFY"]


def test_analyze_reader_keeps_sandbox_fallback_band(tmp_path):
    path = _write_screen(tmp_path, [_row("INFY", "broker"), _row("TCS", "sandbox_default")])
    names = [r["symbol"] for r in read_screen(path, NOW, 10, allow_sandbox_band=True)]
    assert names == ["INFY", "TCS"]


def test_row_without_band_source_is_treated_as_broker(tmp_path):
    # Files written before this change carry no band_source. They were broker bands.
    path = _write_screen(tmp_path, [_row("INFY")])
    assert [r["symbol"] for r in read_screen(path, NOW, 10)] == ["INFY"]


def test_screen_rows_carries_band_source_into_the_file():
    payload = {"candidates": [
        {"symbol": "INFY", "selected": True, "circuit_pct": 0.1, "asm_gsm": False,
         "band_source": "sandbox_default"},
        {"symbol": "TCS", "selected": True, "circuit_pct": 0.1, "asm_gsm": False},
    ]}
    doc = screen_rows(payload, as_of="2026-11-27T10:00:00")
    by = {r["symbol"]: r for r in doc["results"]}
    assert by["INFY"]["band_source"] == "sandbox_default"
    assert by["TCS"]["band_source"] == "broker"


# ---- items 12 / 18: preflight probe must be a tradable equity -------------------------------

def test_preflight_default_probe_is_not_an_index():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "supervise_bot.py"
    spec = importlib.util.spec_from_file_location("supervise_bot_under_test", path)
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)
    default = inspect.signature(sb.build_gates).parameters["symbol"].default
    assert default == "RELIANCE"
    assert "NIFTY" not in default


# ---- item 19: margin_for_order validates direction ------------------------------------------

@pytest.mark.parametrize("bad", ["", "HOLD", "buy-ish", None])
def test_margin_rejects_a_direction_that_is_not_buy_or_sell(bad):
    calls = []
    client = OpenAlgoReadOnly("http://x/", lambda url, payload: calls.append(payload) or {"ok": True})
    with pytest.raises(ValueError):
        client.margin_for_order("K", "INFY", "NSE", bad, 1, "MIS")
    assert calls == []   # nothing is sent


def test_margin_accepts_buy_and_sell_any_case():
    calls = []
    client = OpenAlgoReadOnly("http://x/", lambda url, payload: calls.append(payload) or {"ok": True})
    client.margin_for_order("K", "INFY", "NSE", "buy", 1, "MIS")
    client.margin_for_order("K", "INFY", "NSE", "SELL", 1, "MIS")
    assert [c["positions"][0]["action"] for c in calls] == ["BUY", "SELL"]
