"""Day-end report (2026-11-27): counts, Full Auto steps, kill events, P&L, IST day grouping.

Rules under test:
1. Only rows whose IST day matches are counted (UTC stamps are converted first).
2. Full Auto step rows and kill events are listed.
3. P&L copies only plain numeric fields from the saved bot state.
4. The report is always marked sandbox. Written as JSON and Markdown.
"""
import json

from quant.audit.log import AuditLog
from quant.reporting import daily_report as dr


def _row_log(path, rows):
    """Write audit rows with explicit timestamps (the AuditLog stamps 'now', so write raw lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = ""
    with open(path, "w", encoding="utf-8") as fh:
        for ts, kind, payload in rows:
            fh.write(json.dumps({"ts": ts, "kind": kind, "payload": payload, "prev": prev, "hash": ""}) + "\n")
            prev = ""


def test_ist_day_conversion():
    # 2026-11-30 03:00 UTC = 08:30 IST same day; 19:00 UTC = 00:30 IST next day.
    assert dr._ist_day("2026-11-30T03:00:00+00:00") == "2026-11-30"
    assert dr._ist_day("2026-11-30T19:00:00+00:00") == "2026-12-01"
    assert dr._ist_day("not a time") == ""


def test_only_the_requested_day_is_counted(tmp_path):
    log = tmp_path / "audit.jsonl"
    _row_log(log, [
        ("2026-11-30T04:00:00+00:00", "bot_order", {}),          # IST 09:30 on 30th
        ("2026-11-30T05:00:00+00:00", "bot_order", {}),          # IST 10:30 on 30th
        ("2026-11-30T19:30:00+00:00", "bot_order", {}),          # IST 1 Dec 01:00 -> not the 30th
    ])
    report = dr.build("2026-11-30", [log])
    assert report["events_by_kind"] == {"bot_order": 2}


def test_full_auto_steps_and_kill_events_are_listed(tmp_path):
    log = tmp_path / "audit.jsonl"
    _row_log(log, [
        ("2026-11-30T03:20:00+00:00", "full_auto_step", {"step": "analyzer", "ok": True, "detail": "ON"}),
        ("2026-11-30T03:25:00+00:00", "full_auto_step", {"step": "login", "ok": False, "detail": "cooldown"}),
        ("2026-11-30T07:00:00+00:00", "kill_switch_tripped", {"reason": "x"}),
    ])
    report = dr.build("2026-11-30", [log])
    assert [s["step"] for s in report["full_auto_steps"]] == ["analyzer", "login"]
    assert report["full_auto_steps"][1]["ok"] is False
    assert report["kill_events"] == ["kill_switch_tripped"]


def test_pnl_copies_only_plain_numbers(tmp_path):
    pnl = tmp_path / "pnl_state.json"
    pnl.write_text(json.dumps({"realized_pnl": 1234.567, "daily_pnl": True,
                               "unrealized_pnl": "12", "secret_token": "abc", "pnl": -40}), encoding="utf-8")
    report = dr.build("2026-11-30", [], pnl)
    assert report["pnl"] == {"realized_pnl": 1234.57, "pnl": -40.0}


def test_missing_inputs_give_empty_but_valid_report(tmp_path):
    report = dr.build("2026-11-30", [tmp_path / "none.jsonl"], tmp_path / "none.json")
    assert report["events_by_kind"] == {} and report["pnl"] == {} and report["full_auto_steps"] == []
    assert report["mode"] == "sandbox"


def test_write_creates_json_and_markdown(tmp_path):
    report = dr.build("2026-11-30", [], None)
    paths = dr.write(report, tmp_path / "reports")
    assert [p.rsplit("/", 1)[-1] for p in paths] == ["2026-11-30.json", "2026-11-30.md"]
    md = (tmp_path / "reports" / "2026-11-30.md").read_text(encoding="utf-8")
    assert "mode: sandbox" in md and "## Full Auto steps" in md


def test_report_never_contains_secret_like_payload_fields(tmp_path):
    log = tmp_path / "audit.jsonl"
    _row_log(log, [("2026-11-30T04:00:00+00:00", "credential_update",
                    {"name": "OPENALGO_PASSWORD", "value": "hunter2", "ok": True})])
    text = json.dumps(dr.build("2026-11-30", [log], None))
    assert "hunter2" not in text
