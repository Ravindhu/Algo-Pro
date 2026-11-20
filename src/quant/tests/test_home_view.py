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
