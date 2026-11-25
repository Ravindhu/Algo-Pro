"""Home-page data: screener file reader and portfolio analysis. No network, no broker."""
import json
from datetime import date, timedelta
from pathlib import Path

from quant.reporting import home_view


def test_missing_screener_file_is_not_run(tmp_path: Path):
    out = home_view.read_screener(tmp_path / "none.json")
    assert out["state"] == "not_run" and out["rows"] == []


def test_screener_lists_only_selected_names(tmp_path: Path):
    path = tmp_path / "last_screen.json"
    path.write_text(json.dumps({
        "as_of": "2026-11-20", "source": "Screener.in saved screen",
        "results": [
            {"symbol": "AAA", "name": "Alpha Ltd", "selected": True, "reason": "passed all rules"},
            {"symbol": "BBB", "name": "Beta Ltd", "selected": False, "reason": "debt too high"},
        ]}), encoding="utf-8")
    out = home_view.read_screener(path)
    assert out["state"] == "ok" and out["count"] == 1
    assert [r["symbol"] for r in out["rows"]] == ["AAA"]


def test_bse_code_is_shown_as_the_broker_ticker(tmp_path: Path):
    """A BSE row shows the OpenAlgo symbol, never the 6-digit code and never a name."""
    path = tmp_path / "last_screen.json"
    path.write_text(json.dumps({"as_of": "x", "source": "s", "results": [
        {"symbol": "500325", "selected": True, "reason": "r"},
        {"symbol": "infy", "selected": True, "reason": "r"},
    ]}), encoding="utf-8")
    out = home_view.read_screener(path, ticker_fn=lambda code: {"500325": "RELIANCE"}.get(code))
    assert [r["symbol"] for r in out["rows"]] == ["RELIANCE", "INFY"]
    assert all("name" not in r for r in out["rows"])


def test_unknown_bse_code_gives_empty_ticker_not_the_number(tmp_path: Path):
    path = tmp_path / "last_screen.json"
    path.write_text(json.dumps({"as_of": "x", "source": "s", "results": [
        {"symbol": "999999", "selected": True, "reason": "r"},
    ]}), encoding="utf-8")
    out = home_view.read_screener(path, ticker_fn=lambda _c: None)
    assert out["rows"][0]["symbol"] == ""


def test_default_paths_include_the_data_screened_dir(tmp_path: Path, monkeypatch):
    """The dashboard used to read only a path the screener never writes.

    Real screens land in ``data/screened/last_screen.json``; the panel reported
    "not run" while 14 rows sat on disk. The newest existing file must win.
    """
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"as_of": "old", "results": [
        {"symbol": "OLD", "name": "Old", "selected": True}]}), encoding="utf-8")
    b.write_text(json.dumps({"as_of": "new", "results": [
        {"symbol": "NEW", "name": "New", "selected": True}]}), encoding="utf-8")
    import os
    import time
    os.utime(a, (time.time() - 500, time.time() - 500))
    monkeypatch.setattr(home_view, "SCREEN_PATHS", (a, b))
    out = home_view.read_screener()
    assert out["state"] == "ok" and out["as_of"] == "new"
    assert [r["symbol"] for r in out["rows"]] == ["NEW"]


def test_no_screen_file_anywhere_is_not_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(home_view, "SCREEN_PATHS", (tmp_path / "x.json", tmp_path / "y.json"))
    out = home_view.read_screener()
    assert out["state"] == "not_run" and out["rows"] == []


def test_ticker_lookup_failure_is_blank_not_fatal(tmp_path: Path):
    path = tmp_path / "last_screen.json"
    path.write_text(json.dumps({"as_of": "x", "source": "s", "results": [
        {"symbol": "500325", "selected": True, "reason": "r", "metrics": {"current_price": 10}},
    ]}), encoding="utf-8")

    def boom(_c):
        raise RuntimeError("no OpenAlgo master")

    out = home_view.read_screener(path, ticker_fn=boom)
    assert out["state"] == "ok" and out["rows"][0]["symbol"] == "" and out["rows"][0]["price"] == 10.0


def test_broken_screener_file_is_an_error_state(tmp_path: Path):
    path = tmp_path / "last_screen.json"
    path.write_text("{not json", encoding="utf-8")
    assert home_view.read_screener(path)["state"] == "error"


def test_analysis_is_cached_for_a_minute(monkeypatch):
    home_view._analysis_cache.update(at=None, value=None)
    calls = []

    def fake_compute(rows_fn):
        calls.append(1)
        return {"state": "needs_nav", "nav_rows": 0, "note": "x"}

    monkeypatch.setattr(home_view, "_compute_analysis", fake_compute)
    home_view.portfolio_analysis(now=1000.0)
    home_view.portfolio_analysis(now=1010.0)   # inside the 60 s window: no recompute
    home_view.portfolio_analysis(now=1100.0)   # after the window: recompute
    home_view._analysis_cache.update(at=None, value=None)
    assert len(calls) == 2


def test_too_few_nav_rows_reports_needs_nav_or_unavailable():
    home_view._analysis_cache.update(at=None, value=None)
    out = home_view.portfolio_analysis(now=1.0, rows_fn=lambda: [{"date": "2026-11-02", "nav": 1.0}])
    home_view._analysis_cache.update(at=None, value=None)
    assert out["state"] in ("needs_nav", "unavailable")


def test_analysis_with_nav_history_reports_twrr_and_sharpe():
    home_view._analysis_cache.update(at=None, value=None)
    rows = []
    nav = 1_000_000.0
    for i in range(1, 40):
        nav *= 1.0 + (0.004 if i % 3 else -0.002)
        rows.append({"date": (date(2026, 10, 1) + timedelta(days=i)).isoformat(), "nav": round(nav, 2),
                     "inception": i == 1})
    out = home_view.portfolio_analysis(now=2000.0, rows_fn=lambda: rows)
    home_view._analysis_cache.update(at=None, value=None)
    if out["state"] == "unavailable":  # Blue Engine SDK not installed in this environment
        return
    assert out["state"] == "ok"
    assert out["twrr_pct"] is not None and out["twrr_pct"] > 0
    assert out["sharpe"] is not None
    assert len(out["nav_path"]) == len(rows) and out["nav_path"][0]["date"] == rows[0]["date"][:10]


def test_screener_rows_carry_price_from_metrics(tmp_path):
    import json
    from quant.reporting import home_view
    path = tmp_path / "last_screen.json"
    path.write_text(json.dumps({"as_of": "x", "source": "s", "results": [
        {"symbol": "AAA", "name": "A", "selected": True, "reason": "r", "metrics": {"current_price": 123.456}},
        {"symbol": "BBB", "name": "B", "selected": True, "reason": "r", "metrics": {}},
    ]}), encoding="utf-8")
    out = home_view.read_screener(path)
    prices = {r["symbol"]: r["price"] for r in out["rows"]}
    assert prices == {"AAA": 123.46, "BBB": None}
