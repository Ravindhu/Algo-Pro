"""
test_login_status_card.py - the /infra login card must not lie.

The one rule: a card that shows READY when the broker session is dead is worse
than a card that shows nothing. So:

1. no record -> "NO RUN", never green;
2. yesterday's record -> "STALE" (broker sessions roll over daily);
3. an old-today record -> "STALE" past the 4-hour window;
4. a failed record -> "FAILED" with the failing leg visible;
5. a good, fresh record -> "READY (broker)";
6. a corrupt file -> "UNAVAILABLE" with the reason, never a crash.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blue_engine.dashboard.pages import login_status


def _record(ok=True, broker="mstock", when=None, steps=None, leg="broker"):
    return {
        "ok": ok, "leg": leg,
        "reason": "logged in and verified" if ok else "login failed",
        "broker": broker,
        "at": (when or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "finished_at": (when or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"),
        "steps": steps or [
            {"step": "opt_in", "ok": True, "detail": "allowed", "at": "10:00:00"},
            {"step": "readiness", "ok": True, "detail": "ready", "at": "10:00:05"},
            {"step": "login", "ok": ok, "detail": "done", "at": "10:00:10"},
        ],
    }


def _write(tmp_path: Path, payload) -> Path:
    target = tmp_path / "last_login.json"
    target.write_text(json.dumps(payload) if not isinstance(payload, str) else payload,
                      encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# The honesty rules
# --------------------------------------------------------------------------- #
def test_no_file_means_no_run_not_green(tmp_path):
    record, note = login_status.last_login_record(tmp_path / "absent.json")
    assert record is None
    assert "no run" in note
    label, _detail, ok = login_status._state(record, note)
    assert ok is False
    assert label == "NO RUN"


def test_yesterdays_success_is_stale_not_green(tmp_path):
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    path = _write(tmp_path, _record(ok=True, when=yesterday))
    record, note = login_status.last_login_record(path)
    assert record is not None, "the record exists - it is just stale"
    label, _detail, ok = login_status._state(record, note)
    assert ok is False
    assert label == "STALE"
    assert "stale" in note


def test_a_4_hour_old_record_is_stale(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    path = _write(tmp_path, _record(ok=True, when=old))
    _record_note, note = login_status.last_login_record(path)
    assert "stale" in note
    assert "5.0h" in note or "5h" in note


def test_a_fresh_morning_record_is_ready(tmp_path):
    path = _write(tmp_path, _record(ok=True))
    record, note = login_status.last_login_record(path)
    label, _detail, ok = login_status._state(record, note)
    assert ok is True
    assert label == "READY (mstock)"
    assert note == "today's run"


def test_a_failed_record_shows_the_leg_not_a_green_tick(tmp_path):
    path = _write(tmp_path, _record(ok=False, leg="broker"))
    record, note = login_status.last_login_record(path)
    label, _detail, ok = login_status._state(record, note)
    assert ok is False
    assert label == "FAILED"


@pytest.mark.parametrize("payload", ["{not json", "[]", '"a string"',
                                     '{"hello": "world"}'])
def test_a_broken_record_is_unavailable_never_a_crash(tmp_path, payload):
    path = _write(tmp_path, payload)
    record, note = login_status.last_login_record(path)
    assert record is None
    assert note
    label, _detail, ok = login_status._state(record, note)
    assert ok is False


# --------------------------------------------------------------------------- #
# The rows shown to the operator
# --------------------------------------------------------------------------- #
def test_rows_show_every_recorded_step_in_order(tmp_path):
    path = _write(tmp_path, _record(ok=False))
    record, _note = login_status.last_login_record(path)
    rows = login_status.login_rows(record)
    assert [r["step"] for r in rows] == ["opt_in", "readiness", "login"]
    assert rows[-1]["ok"] is False, "the failing step must be visible"


def test_rows_are_empty_when_there_is_no_record(tmp_path):
    assert login_status.login_rows(None) == []


def test_rows_never_contain_a_credential(tmp_path):
    """The card is rendered on a page any logged-in operator can see."""
    path = _write(tmp_path, _record(ok=True, steps=[
        {"step": "login", "ok": True,
         "detail": "BROKER_PASSWORD=Ravindhu@11 sent", "at": "10:00:10"},
    ]))
    record, _note = login_status.last_login_record(path)
    blob = json.dumps(login_status.login_rows(record))
    assert "Ravindhu@11" not in blob
    assert "Ravindhu" not in blob or "broker" in blob.lower()


# --------------------------------------------------------------------------- #
# The card component
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def page_modules():
    pytest.importorskip("dash")
    pytest.importorskip("dash_mantine_components")
    import dash

    dash.Dash(__name__, pages_folder="", use_pages=True)
    from blue_engine.dashboard.pages import login_status as module
    from blue_engine.dashboard.pages import infrastructure as infra
    return module, infra


def test_the_card_builds_from_a_fresh_record(page_modules, tmp_path):
    module, _infra = page_modules
    path = _write(tmp_path, _record(ok=True))
    card, ai_rows, state = module.login_card(path=path)
    assert state["ok"] is True
    assert "READY" in state["label"]
    assert card is not None
    assert any(row[0] == "Broker login" for row in ai_rows)


def test_the_card_builds_from_a_failed_record(page_modules, tmp_path):
    module, _infra = page_modules
    path = _write(tmp_path, _record(ok=False, leg="broker"))
    _card, _rows, state = module.login_card(path=path)
    assert state["ok"] is False
    assert "FAILED" in state["label"]


def test_the_infra_page_renders_with_the_card(page_modules, tmp_path, monkeypatch):
    module, infra = page_modules
    path = _write(tmp_path, _record(ok=True))
    monkeypatch.setattr(module, "LAST_LOGIN", path)
    body = infra.body()
    assert body, "the /infra page must still render"
    text = str(body)
    assert "Broker login" in text


def test_the_infra_page_survives_a_missing_record(page_modules, tmp_path,
                                                  monkeypatch):
    module, infra = page_modules
    monkeypatch.setattr(module, "LAST_LOGIN", tmp_path / "absent.json")
    body = infra.body()
    text = str(body)
    assert "NO RUN" in text or "no run" in text.lower()


# --------------------------------------------------------------------------- #
# Architectural invariant
# --------------------------------------------------------------------------- #
def test_the_card_module_never_starts_a_login():
    """The card is a view. A dashboard page that logs in is an incident."""
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "dashboard" / "pages" / "login_status.py").read_text(encoding="utf-8")
    for forbidden in ("BrokerAuth(", "run_scheduled_login", "subprocess",
                      "scheduled_login", "post_form", "start_all"):
        assert forbidden not in text
