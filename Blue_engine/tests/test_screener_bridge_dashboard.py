"""Screener page: bridge status tiles must render (and never raise).

Offline: every reader is pointed at a temp directory, so nothing depends on
a bridge having run on this machine.
"""
from __future__ import annotations

import json

import dash
import pytest

# The page calls ``dash.register_page`` at import time, which requires an app.
# Neuter it before importing (same pattern as test_ai_insight_all_pages).
_orig_register_page = dash.register_page
dash.register_page = lambda *a, **k: None
from blue_engine.dashboard.pages import screener as page  # noqa: E402
dash.register_page = _orig_register_page


@pytest.fixture
def page_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(page, "_BRIDGE_REPORT_PATH", tmp_path / "report.json")
    monkeypatch.setattr(page, "_PROTECTIVE_PATH", tmp_path / "levels.json")
    return tmp_path


def test_bridge_absent_renders_honest_empty_state(page_paths):
    text = str(page.body())
    assert "AI INSIGHT" in text
    assert "No bridge run report yet" in text


def test_bridge_report_and_levels_render(page_paths):
    (page_paths / "report.json").write_text(json.dumps({
        "as_of": "2026-10-21T10:00:00+05:30",
        "exchange": "NSE",
        "symbols": [{"symbol": "INFY", "exchange": "NSE"},
                    {"symbol": "500319", "exchange": "BSE"}],
        "active": ["INFY|NSE"],
        "disabled": {"500319|BSE": "init error: no token"},
        "open_positions": {"INFY|NSE": 10},
        "executed": [
            {"ts": "2026-10-21T09:30:00", "symbol": "INFY", "exchange": "NSE",
             "status": "executed", "intent": {"action": "BUY", "quantity": 10,
                                              "stop_loss": 95.0, "target": 115.0},
             "summary": {"filled": 10, "child_orders": 2}},
            {"ts": "2026-10-21T09:31:00", "symbol": "500319", "exchange": "BSE",
             "status": "risk_gate_block", "intent": {}, "summary": None},
        ],
    }), encoding="utf-8")
    (page_paths / "levels.json").write_text(json.dumps({
        "exec-1|INFY": {"execution_id": "exec-1", "symbol": "INFY",
                        "exchange": "NSE", "action": "BUY",
                        "stop_loss": 95.0, "target": 115.0, "strategy": "OCL",
                        "exit_mode": "FIXED", "htf_stop_loss": 90.0,
                        "registered_at": "2026-10-21T09:30:00"},
    }), encoding="utf-8")
    text = str(page.body())
    assert "Bridge - screener to execution" in text
    assert "INFY" in text and "500319" in text
    assert "no token" in text                 # disabled reason surfaced
    assert "risk gate block" in text          # execution category
    assert "95.00" in text and "115.00" in text  # protective levels
    assert "Disabled symbols (fail-closed)" in text


def test_corrupt_files_degrade_to_empty_state(page_paths):
    (page_paths / "report.json").write_text("{not json", encoding="utf-8")
    (page_paths / "levels.json").write_text("", encoding="utf-8")
    text = str(page.body())
    assert "No bridge run report yet" in text
