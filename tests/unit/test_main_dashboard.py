"""Main dashboard (decision 0027): dashboard/main_dashboard.html is what /ui serves.

Text checks plus a Node syntax check, so they run without extra packages.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = ROOT / "dashboard" / "main_dashboard.html"


def test_ui_serves_the_main_dashboard():
    text = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert '"page": ROOT / "dashboard" / "main_dashboard.html"' in text


def test_page_has_trade_setup_panel():
    text = PAGE.read_text(encoding="utf-8")
    for element_id in ("setupbtn", "setup", "setupform", "su-risk", "su-symbols", "su-strategy", "setupsave", "token"):
        assert f'id="{element_id}"' in text, element_id


def test_page_reads_and_writes_only_setup_routes():
    text = PAGE.read_text(encoding="utf-8")
    routes = set(re.findall(r'"(/api/[\w/]+)"', text))
    assert routes == {"/api/setup"}
    assert text.count('method: "POST"') == 1
    assert '"X-Dashboard-Token"' in text


def test_page_never_stores_the_token():
    text = PAGE.read_text(encoding="utf-8")
    for store in ("localStorage", "sessionStorage", "document.cookie"):
        assert store not in text, store


def test_page_has_no_order_code_or_simulated_random():
    text = PAGE.read_text(encoding="utf-8")
    for banned in ("placeorder", "Math.random"):
        assert banned not in text, banned


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_main_dashboard_script_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)
    assert scripts
    for i, sc in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(sc, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
