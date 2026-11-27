"""Owner sign-in job: Screener.in session first, then the saved-screen scrape when stale.

Rules under test (2026-11-27):
1. A missing or stale last_screen.json is scraped once after the owner signs in.
2. A fresh last_screen.json is NOT scraped again (the bot's 60-minute rule still applies).
3. No session means no scrape.
4. A scrape failure is contained and audited; it never raises.
5. Every step is audited with the actor.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from quant.data import screen_refresh as sr


def _rows(path):
    return [json.loads(line)["payload"] for line in path.read_text(encoding="utf-8").splitlines()
            if "screener_scrape_after_signin" in line]


def _session_ok(**_):
    return {"ok": True, "saved": False, "reason": "session reused from today"}


def test_missing_selection_is_scraped_once(tmp_path):
    audit, source = tmp_path / "a.jsonl", tmp_path / "last_screen.json"
    calls = []

    def scrape():
        calls.append(1)
        return {"ok": True, "count": 3, "reason": "3 selected"}

    out = sr.owner_signin_job("Owner", audit, source, session_runner=_session_ok, scrape=scrape)
    assert out["ran"] is True and calls == [1]
    row = _rows(audit)[-1]
    assert row["ran"] is True and row["ok"] is True and row["count"] == 3 and row["actor"] == "Owner"


def test_fresh_selection_is_not_scraped_again(tmp_path):
    audit, source = tmp_path / "a.jsonl", tmp_path / "last_screen.json"
    source.write_text("{}", encoding="utf-8")  # just written: fresh
    calls = []
    out = sr.owner_signin_job("Owner", audit, source, session_runner=_session_ok,
                              scrape=lambda: calls.append(1) or {"ok": True})
    assert out["ran"] is False and calls == []
    assert _rows(audit)[-1]["reason"] == "saved selection is fresh"


def test_stale_selection_is_scraped(tmp_path):
    audit, source = tmp_path / "a.jsonl", tmp_path / "last_screen.json"
    source.write_text("{}", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(source, (old, old))
    calls = []
    sr.owner_signin_job("Owner", audit, source, session_runner=_session_ok,
                        scrape=lambda: calls.append(1) or {"ok": True, "count": 0})
    assert calls == [1]


def test_no_session_means_no_scrape(tmp_path):
    audit, source = tmp_path / "a.jsonl", tmp_path / "last_screen.json"
    calls = []
    out = sr.owner_signin_job("Owner", audit, source,
                              session_runner=lambda: {"ok": False, "reason": "login refused"},
                              scrape=lambda: calls.append(1) or {"ok": True})
    assert out["ran"] is False and calls == []
    assert "session not usable" in _rows(audit)[-1]["reason"]


def test_scrape_exception_is_contained_and_audited(tmp_path):
    audit, source = tmp_path / "a.jsonl", tmp_path / "last_screen.json"

    def boom():
        raise RuntimeError("network down")

    out = sr.owner_signin_job("Owner", audit, source, session_runner=_session_ok, scrape=boom)
    assert out["ran"] is True and out["scrape"]["ok"] is False
    assert _rows(audit)[-1]["ok"] is False


def test_source_is_stale_fails_closed(tmp_path):
    now = datetime.now(timezone.utc)
    assert sr.source_is_stale(tmp_path / "missing.json", now, 60) is True
    f = tmp_path / "x.json"
    f.write_text("{}", encoding="utf-8")
    assert sr.source_is_stale(f, now, 0) is True
    assert sr.source_is_stale(f, now + timedelta(hours=2), 60) is True


def test_dashboard_shows_the_scraped_candidates(tmp_path, monkeypatch):
    """A sign-in scrape must show its names on the dashboard, not 'the last run selected no names'."""
    from quant.reporting import home_view

    monkeypatch.setattr(sr, "ensure_session", lambda **_: {"usable": True, "action": "test", "reason": ""})
    source = tmp_path / "last_screen.json"
    engine_payload = {"ok": True, "warnings": [], "data": {
        "results": [{"symbol": "INFY", "selected": True, "metrics": {"current_price": 1500.5}}],
        "selected": ["INFY"]}}
    out = sr.make_screener_scraper(source=source, run_engine=lambda: engine_payload)()
    assert out["ok"] is True

    monkeypatch.setattr(home_view, "SCREEN_PATHS", (source, tmp_path / "latest.json"))
    panel = home_view.read_screener()
    assert panel["state"] == "ok"
    assert [r["symbol"] for r in panel["rows"]] == ["INFY"]
    assert panel["rows"][0]["price"] == 1500.5
