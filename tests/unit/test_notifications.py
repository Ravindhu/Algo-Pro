"""Execution log > Notification tab: the reader and the page wiring (2026-11-27).

Rules under test:
1. Every bot alert is listed, with its level and message.
2. The warning kinds the bot writes are listed; ordinary rows are not.
3. Newest first, capped; a bad line or a missing file never raises.
4. The page has the two tabs and calls the renderer from the overview.
"""
import json
from pathlib import Path

from quant.reporting import notifications as nt

PAGE = Path(__file__).resolve().parents[2] / "dashboard" / "main_dashboard.html"


def _row(kind, ts, **payload):
    return json.dumps({"ts": ts, "kind": kind, "payload": payload, "prev": None, "hash": "x"})


def test_alerts_and_warning_kinds_are_listed_newest_first(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join([
        _row("pnl_day_reset", "2026-11-27T03:00:00+00:00", day="2026-11-27"),
        _row("alert", "2026-11-27T04:00:00+00:00", level="WARNING", component="x", message="screen stale"),
        _row("exit_held_stale_bar", "2026-11-27T05:00:00+00:00", symbol="INFY", age_cycles=5),
        _row("screen_refresh", "2026-11-27T05:30:00+00:00", result={"exit_code": 2, "reason": "no candidates"}),
        _row("screen_refresh", "2026-11-27T05:35:00+00:00", result={"exit_code": 0}),
        "not json at all",
    ]) + "\n", encoding="utf-8")

    out = nt.recent([audit])
    assert out["count"] == 3
    levels = [(i["source"], i["level"]) for i in out["items"]]
    assert levels == [("screen_refresh", "WARNING"), ("exit_held_stale_bar", "CRITICAL"), ("alert", "WARNING")]
    assert out["items"][0]["message"] == "screen refresh: no candidates"
    assert out["items"][1]["message"].startswith("exit held stale bar")
    assert "INFY" in out["items"][1]["message"]
    assert out["items"][2]["message"] == "screen stale"
    assert out["items"][2]["time_ist"] == "2026-11-27 09:30:00"  # 04:00 UTC is 09:30 IST


def test_cap_and_missing_file_are_safe(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(_row("alert", f"2026-11-27T0{i % 9}:00:00+00:00", level="CRITICAL",
                                    message=f"m{i}") for i in range(60)) + "\n", encoding="utf-8")
    assert nt.recent([audit], limit=50)["count"] == 50
    assert nt.recent([tmp_path / "nope.jsonl"]) == {"count": 0, "items": []}


def test_healthy_recon_is_not_a_notification(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(_row("startup_recon", "2026-11-27T01:00:00+00:00", status="match") + "\n",
                     encoding="utf-8")
    assert nt.recent([audit])["count"] == 0


def test_page_has_the_two_tabs_and_renders_the_list():
    text = PAGE.read_text(encoding="utf-8")
    for element_id in ("tab-fills", "tab-notify", "fills-view", "notify-view", "notify-rows", "notify-count"):
        assert f'id="{element_id}"' in text, element_id
    assert "function renderNotifications" in text
    assert "renderNotifications(o.notifications)" in text
