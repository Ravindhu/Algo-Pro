"""P&L shown by default (decisions 0007 and 0017). The flow popup colours live node status (decisions 0020 and 0030).

PAGE is the main dashboard served at /ui (decision 0027). The old page src/quant/index.html was
removed on 2026-11-07; its checks now run on the main page.
Web tests are text checks plus a Node syntax check, so they run without extra packages.
"""
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = ROOT / "dashboard" / "main_dashboard.html"
FLOW_JS = ROOT / "dashboard" / "assets" / "flow_svg.js"


def test_pnl_visible_by_default_in_settings():
    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert data["dashboard"]["pnl_visible_default"] is True


def test_page_starts_from_the_settings_default():
    text = PAGE.read_text(encoding="utf-8")
    assert "showMoney = !!o.settings.pnl_visible_default" in text
    assert "showMoney = false" not in text


def test_page_has_a_hide_show_toggle_for_money():
    text = PAGE.read_text(encoding="utf-8")
    assert 'id="toggle"' in text
    assert "showMoney = !showMoney" in text


def test_old_pages_are_gone():
    assert not (ROOT / "dashboard" / "index.html").exists()
    assert not (ROOT / "src" / "quant" / "index.html").exists()


def test_page_has_no_order_code_or_simulated_data():
    text = PAGE.read_text(encoding="utf-8")
    for banned in ("placeorder", "Math.random", "open.push"):
        assert banned not in text, banned


def test_page_reads_overview_and_writes_only_known_routes():
    text = PAGE.read_text(encoding="utf-8")
    assert 'fetch("/api/overview"' in text
    routes = set(re.findall(r'"(/api/[\w/-]+)"', text))
    assert routes == {"/api/overview", "/api/limits", "/api/kill/trip", "/api/kill/reset", "/api/setup",
                      "/api/trading-mode", "/api/loop/start", "/api/loop/stop", "/api/portfolio"}  # portfolio: read-only (decision 0032)
    # saveLimits, postKill, saveSetup, postMode, postLoop only
    assert text.count('method: "POST"') == 5
    assert '"X-Dashboard-Token"' in text


def test_page_has_kill_switch_controls():
    text = PAGE.read_text(encoding="utf-8")
    for element_id in ("killtrip", "killreset", "killreason", "killmsg", "token"):
        assert f'id="{element_id}"' in text, element_id
    assert "confirm(" in text  # both kill actions ask first


def test_page_never_stores_the_token():
    text = PAGE.read_text(encoding="utf-8")
    for store in ("localStorage", "sessionStorage", "document.cookie"):
        assert store not in text, store


def test_page_has_an_input_for_every_risk_limit():
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from quant.core.limits_store import WRITABLE

    text = PAGE.read_text(encoding="utf-8")
    covered = set(re.findall(r'data-limit="(\w+)"', text))
    assert covered == set(WRITABLE)  # every saved limit has an input; no rupee inputs


def test_flow_asset_shows_new_kill_switch_wording():
    js = FLOW_JS.read_text(encoding="utf-8")
    assert "Blocks entries, closes all" in js
    assert "Blocks entries at risk gate" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_flow_page_script_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)
    assert scripts
    for i, sc in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(sc, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
