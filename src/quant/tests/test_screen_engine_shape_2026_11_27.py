"""Regression for the one unverified link of the screen chain (2026-11-27).

The loader keeps rows whose ``selected`` key is truthy. This test builds the engine's OWN row
objects (``ScreenResult.as_dict()``, the same call ``screen_now`` makes), not hand-written dicts,
and drives them through load_candidates -> refresh_once -> latest.json -> read_screen.
Offline: fake quotes, no broker, no screener.in.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quant.data.screen_refresh import load_candidates, make_screen_refresher
from quant.engines.screener.filters import ScreenResult
from quant.execution.strategy_bot import read_screen

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 27, 10, 0, tzinfo=IST)


class _Reader:
    def quotes(self, api_key, symbol, exchange):
        return {"status": "success", "data": {"upper_circuit_limit": 110.0, "lower_circuit_limit": 90.0,
                                              "prev_close": 100.0, "asm": False, "gsm": False}}


def _engine_payload():
    """Exactly the shape screen_now() returns: {ok, data: {results: [ScreenResult.as_dict(), ...]}}."""
    rows = [
        ScreenResult(symbol="INFY", selected=True, name="Infosys"),
        ScreenResult(symbol="TCS", selected=False, name="TCS", excluded_by="sector gate"),
    ]
    return {"ok": True, "data": {"results": [r.as_dict() for r in rows]}, "warnings": []}


def test_engine_row_carries_selected_key():
    """The loader depends on this key; if the engine stops writing it, the bot gets zero candidates."""
    for row in _engine_payload()["data"]["results"]:
        assert "selected" in row and isinstance(row["selected"], bool)


def test_real_engine_rows_only_selected_reach_the_bot(tmp_path):
    source = tmp_path / "last_screen.json"
    source.write_text(json.dumps(_engine_payload()), encoding="utf-8")

    assert [r["symbol"] for r in load_candidates(source)] == ["INFY"]

    target = tmp_path / "latest.json"
    refresh = make_screen_refresher(_Reader(), "key", source=source, target=target)
    out = refresh(NOW)
    assert out["exit_code"] == 0, out
    assert out["wrote"] == 1

    assert [r["symbol"] for r in read_screen(target, NOW, 10)] == ["INFY"]


def test_empty_engine_payload_does_not_overwrite_last_good_screen(tmp_path, monkeypatch):
    """Audit 2026-11-27: URL_SCREENER_DOT_IN unset -> engine says ok=True with no rows and a warning.
    The scraper must refuse that and keep the last good selection on disk."""
    import quant.data.screen_refresh as sr

    monkeypatch.setattr(sr, "ensure_session", lambda **_: {"usable": True, "action": "test", "reason": ""})
    source = tmp_path / "last_screen.json"
    good = _engine_payload()
    source.write_text(json.dumps(good), encoding="utf-8")
    empty = {"ok": True, "data": {}, "warnings": ["source='screen' requires screen_id and screen_slug"]}

    out = sr.make_screener_scraper(source=source, run_engine=lambda: empty)()

    assert out["ok"] is False
    assert "requires screen_id" in out["reason"]
    assert json.loads(source.read_text(encoding="utf-8")) == good


def test_real_engine_payload_is_written_by_scraper(tmp_path, monkeypatch):
    import quant.data.screen_refresh as sr

    monkeypatch.setattr(sr, "ensure_session", lambda **_: {"usable": True, "action": "test", "reason": ""})
    source = tmp_path / "last_screen.json"
    out = sr.make_screener_scraper(source=source, run_engine=_engine_payload)()

    assert out["ok"] is True
    assert [r["symbol"] for r in load_candidates(source)] == ["INFY"]
