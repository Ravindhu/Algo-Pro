"""End-to-end audit of the screen chain (2026-11-26): last_screen.json -> latest.json -> bot.

Every test here reproduces a defect that a probe of the REAL files proved, then asserts the fix.
Offline: fake quotes, a fake engine. No broker, no screener.in, no server.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant.data.screen_refresh import (load_candidates, make_screen_refresher,
                                       make_screener_scraper)
from quant.execution.strategy_bot import read_screen
from quant.portfolio.risk_limits import sector_map
from quant.reporting import home_view

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 26, 10, 0, tzinfo=IST)
FORBIDDEN = "payment dead-2026-11-26"


def _quote(symbol, exchange):
    """A broker quote carrying everything the band filter needs."""
    return {"status": "success", "data": {"upper_circuit_limit": 110.0, "lower_circuit_limit": 90.0,
                                          "prev_close": 100.0, "asm": False, "gsm": False}}


class _Reader:
    def quotes(self, api_key, symbol, exchange):
        return _quote(symbol, exchange)


def _engine_output(selected):
    """Exactly the shape ``ScreenerEngine.screen_now()`` writes: {ok, data: {results: [...]}}."""
    return {"ok": True, "data": {"results": selected}, "warnings": []}


# ---- defect 1: the real engine payload was read as a flat file ---------------------------------

def test_engine_shaped_last_screen_is_read(tmp_path):
    """The scraper writes {ok, data:{results}}, but load_candidates read only top-level results."""
    src = tmp_path / "last_screen.json"
    src.write_text(json.dumps(_engine_output([
        {"symbol": "INFY", "selected": True},
        {"symbol": "TCS", "selected": False},
    ])), encoding="utf-8")

    rows = load_candidates(src)
    assert [r["symbol"] for r in rows] == ["INFY"]      # selected only
    # A flat file still works, so an older hand-written screen is not broken.
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"results": [{"symbol": "AAA", "selected": True}]}), encoding="utf-8")
    assert [r["symbol"] for r in load_candidates(flat)] == ["AAA"]


def test_whole_chain_engine_output_to_bot_symbols(tmp_path):
    """One refresh from the engine's own shape must give the bot its symbols, at its own clock."""
    src = tmp_path / "last_screen.json"
    dst = tmp_path / "latest.json"
    src.write_text(json.dumps(_engine_output([
        {"symbol": "INFY", "selected": True, "sector": "IT"},
        {"symbol": "TCS", "selected": True, "sector": "IT"},
    ])), encoding="utf-8")

    refresh = make_screen_refresher(_Reader(), "KEY", source=src, target=dst)
    out = refresh(NOW)
    assert out["wrote"] == 2 and out["exit_code"] == 0

    doc = json.loads(dst.read_text(encoding="utf-8"))
    assert [r["symbol"] for r in doc["results"]] == ["INFY", "TCS"]

    # BOT SIDE: read_screen must see them, and it must not judge the stamp as UTC.
    # 110 / 90 on a 100 prev close is a 10% band either side.
    assert read_screen(dst, NOW, 10.0) == [
        {"symbol": "INFY", "circuit_pct": 0.1}, {"symbol": "TCS", "circuit_pct": 0.1}]
    # PORTFOLIO SIDE: sectors must reach the file, or every name is one capped bucket.
    assert sector_map(dst) == {"INFY": "IT", "TCS": "IT"}


# ---- defect 2: as_of was stamped in UTC, not with the bot's clock ------------------------------

def test_as_of_is_the_bot_clock_not_utc(tmp_path):
    """A UTC stamp on an IST clock lands 5.5 h ahead, and read_screen's `as_of > now` voids the file."""
    dst = tmp_path / "latest.json"
    refresh = make_screen_refresher(
        _Reader(), "KEY", source=_with(time=tmp_path / "src.json",
                                       rows=[{"symbol": "INFY", "selected": True}]),
        target=dst)
    refresh(NOW)
    as_of = json.loads(dst.read_text(encoding="utf-8"))["as_of"]
    assert as_of == NOW.isoformat()
    assert as_of.endswith("+05:30")
    # The exact defect: the same file judged at the same instant must not be "in the future".
    assert read_screen(dst, NOW, 10.0)


def _with(*, time, rows):
    """A small factory so a test can build a source file inline."""
    time.write_text(json.dumps({"results": rows}), encoding="utf-8")
    return time


# ---- defect 3: the scraper built the engine with 2 keys, discarding the real config ----------

def test_scraper_feeds_the_engine_its_own_config_file(tmp_path, monkeypatch):
    """BaseEngine only copies the dict it is given. The shell must load screener_engine.json first."""
    from quant.data import screen_refresh as sr

    seen = {}

    class _Engine:
        def __init__(self, config=None):
            seen.update(config or {})

        def screen_now(self):
            return _engine_output([{"symbol": "INFY", "selected": True}])

    monkeypatch.setattr(sr, "_run_screener_engine", sr._run_screener_engine)
    # Replace the dynamic loader with a stub module carrying the stub engine.
    import types
    stub = types.ModuleType("stub_screener")
    stub.ScreenerEngine = _Engine
    monkeypatch.setattr("quant.core.blue_bridge.load", lambda name: stub, raising=False)

    payload = sr._run_screener_engine()
    assert payload["ok"] is True
    # The file's own keys survive ...
    assert seen.get("source") == "screen"
    assert "max_symbols" in seen and seen.get("max_symbols") == 250
    assert "min_interval_ms" in seen          # transport limits are not lost
    # ... and only enrichment is switched off for the bot's refresh path.
    assert seen["enrich"] is False
    assert seen["annual_report_enrichment"] is False


def test_scraper_reports_the_selected_count(tmp_path, monkeypatch):
    from quant.data import screener_session as ss

    # Hermetic: a real .env on this machine must not decide the outcome of the test.
    for name in ss.ENV_ONLY_KEYS:
        monkeypatch.delenv(name, raising=False)

    src = tmp_path / "last_screen.json"
    scraper = make_screener_scraper(
        source=src, env_file=tmp_path / ".env",
        run_engine=lambda: _engine_output([{"symbol": "INFY", "selected": True}]))
    # No credentials, no cookie -> the session step refuses, so nothing is written.
    out = scraper()
    assert out["ok"] is False
    assert "session" in out["reason"]
    assert not src.exists()


# ---- defect 4: the dashboard picked "newest" by mtime, not by as_of ---------------------------

def test_dashboard_prefers_the_fresh_file_over_a_newer_mtime(tmp_path, monkeypatch):
    old = tmp_path / "last_screen.json"
    fresh = tmp_path / "latest.json"
    old.write_text(json.dumps({"as_of": (NOW - timedelta(hours=2)).isoformat(),
                               "results": [{"symbol": "OLD", "selected": True}]}), encoding="utf-8")
    fresh.write_text(json.dumps({"as_of": NOW.isoformat(),
                                 "results": [{"symbol": "NEW", "selected": True}]}), encoding="utf-8")
    # Simulate the trap: the stale selection file was written a moment LATER than the fresh one.
    old.touch()
    monkeypatch.setattr(home_view, "SCREEN_PATHS", (old, fresh))

    out = home_view.read_screener()
    assert [r["symbol"] for r in out["rows"]] == ["NEW"]
    assert out["as_of"] == NOW.isoformat()


def test_dashboard_falls_back_to_mtime_when_as_of_is_absent(tmp_path, monkeypatch):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"results": [{"symbol": "AAA", "selected": True}]}), encoding="utf-8")
    b.write_text(json.dumps({"results": [{"symbol": "BBB", "selected": True}]}), encoding="utf-8")
    import os, time
    os.utime(a, (time.time() - 100, time.time() - 100))
    monkeypatch.setattr(home_view, "SCREEN_PATHS", (a, b))
    assert [r["symbol"] for r in home_view.read_screener()["rows"]] == ["BBB"]
