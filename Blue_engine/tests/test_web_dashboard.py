"""
Tests for the FastAPI dashboard (dashboard/web).

* Builder and static-asset tests run without FastAPI.
* Route tests need FastAPI and are skipped where it is not installed.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from blue_engine.dashboard.web import sections

STATIC = Path(sections.__file__).resolve().parent / "static"

EXPECTED_TITLES = [
    "Terminal", "Portfolio", "Risk Control", "Analytics", "Infrastructure",
    "Economic", "Sectors", "Portfolio Analytics", "ML Models", "Optimizer",
    "Screener", "Agent Line",
]


# ------------------------------------------------------------ section list
def test_header_has_the_twelve_page_titles_in_order():
    assert [s["title"] for s in sections.section_titles()] == EXPECTED_TITLES


def test_section_keys_are_unique_and_url_safe():
    keys = [s["key"] for s in sections.section_titles()]
    assert len(keys) == len(set(keys)) == 12
    assert all(k.replace("_", "").isalnum() for k in keys)


def test_every_header_button_has_a_builder():
    for item in sections.section_titles():
        payload = sections.build_section(item["key"])
        assert payload is not None, item["key"]
        assert payload["key"] == item["key"]
        assert payload["title"] == item["title"]


def test_unknown_section_returns_none():
    assert sections.build_section("does-not-exist") is None


# ------------------------------------------------------------ payload shape
@pytest.mark.parametrize("key", [k for k, _ in sections.SECTIONS])
def test_payload_shape_is_stable(key):
    payload = sections.build_section(key)
    assert set(payload) == {"key", "title", "status", "summary", "tables", "notes", "error"}
    assert payload["status"] in {"ok", "empty", "error"}
    assert isinstance(payload["summary"], list)
    assert isinstance(payload["tables"], list)
    json.dumps(payload)  # must be JSON-serialisable for the browser


# ------------------------------------------------------------ fail-soft
def test_a_failing_source_becomes_an_error_payload_not_an_exception(monkeypatch):
    def boom():
        raise RuntimeError("ledger offline")

    monkeypatch.setattr(sections, "_gather", boom)
    payload = sections.build_section("terminal")
    assert payload["status"] == "error"
    assert "ledger offline" in payload["error"]


# ------------------------------------------------------------ secrets
def test_secret_looking_keys_are_never_rendered():
    summary, tables = [], []
    sections._flatten(
        {"symbol": "INFY", "api_key": "abc123", "nested": {"broker_password": "x", "qty": 5}},
        summary=summary, tables=tables)
    labels = {row["label"] for row in summary}
    assert "symbol" in labels and "nested.qty" in labels
    assert not any("api_key" in lbl or "password" in lbl for lbl in labels)
    assert "abc123" not in json.dumps(summary)


def test_record_lists_become_capped_tables():
    rows = [{"n": i} for i in range(sections._MAX_ROWS + 10)]
    summary, tables = [], []
    sections._flatten({"trades": rows}, summary=summary, tables=tables)
    table = tables[0]
    assert table["total"] == len(rows)
    assert len(table["rows"]) == sections._MAX_ROWS


# ------------------------------------------------------------ formatting
def test_fmt_handles_none_bool_float_and_long_text():
    assert sections._fmt(None) == "-"
    assert sections._fmt(True) == "yes"
    assert sections._fmt(1.5) == "1.5"
    assert sections._fmt(2.0) == "2"
    assert len(sections._fmt("x" * 500)) == sections._MAX_TEXT


# ------------------------------------------------------------ static assets
def test_static_assets_exist():
    for name in ("index.html", "app.js", "style.css", "neural_shell.svg"):
        assert (STATIC / name).is_file(), name


def test_index_wires_the_nav_dialog_and_shell_image():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="nav"' in html
    assert 'id="popup"' in html and "<dialog" in html
    assert "/static/neural_shell.svg" in html
    assert "/static/app.js" in html


def test_client_never_injects_server_data_as_html():
    import re
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\.innerHTML\s*[+]?=", js)
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js


def test_neural_shell_is_well_formed_svg_with_full_flow_labels():
    root = ET.parse(STATIC / "neural_shell.svg").getroot()
    assert root.tag.endswith("svg")
    text = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("text"))
    for stage in ("NSE/BSE", "Economic", "Alpha", "Strategy", "POV slicer",
                  "Risk gate", "Kill switch", "OpenAlgo bridge", "Broker", "Exchange"):
        assert stage in text, stage


# ------------------------------------------------------------ FastAPI routes
def test_routes_serve_the_dashboard_and_read_only_api():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from blue_engine.dashboard.web.app import create_app

    client = TestClient(create_app())
    assert client.get("/healthz").json() == {"status": "ok"}
    index = client.get("/")
    assert index.status_code == 200 and "POV Terminal" in index.text
    listing = client.get("/api/sections").json()["sections"]
    assert [s["title"] for s in listing] == EXPECTED_TITLES
    assert client.get("/api/sections/risk").json()["key"] == "risk"
    assert client.get("/api/sections/nope").status_code == 404
    # read-only: no write verbs are exposed
    assert client.post("/api/sections/risk").status_code == 405


# ------------------------------------------------------------ read-only guard
_EXECUTION_MODULES = ("blue_engine.core.client", "blue_engine.pov", "blue_engine.core.runner",
                      "blue_engine.core.paper_broker", "openalgo")


def test_dashboard_code_never_imports_an_execution_path():
    """The dashboard must stay read-only: no broker, POV or runner imports."""
    import ast

    web_dir = Path(sections.__file__).resolve().parent
    files = list(web_dir.glob("*.py")) + [web_dir.parent / "data_layer.py"]
    for path in files:
        # utf-8-sig: some source files start with a byte-order mark
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(name == m or name.startswith(m + ".") for m in _EXECUTION_MODULES), (
                    path.name, name)
