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
    assert "showMoney = false" not in text


def test_money_is_always_visible_no_toggle():
    # Owner request 2026-11-09: no Hide/Show button; money always shown.
    text = PAGE.read_text(encoding="utf-8")
    assert 'id="toggle"' not in text
    assert "••••" not in text
    assert "const showMoney = true" in text


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
    # Kill switch removed from the page (owner request 2026-11-09). Backend routes stay in app.py.
    # Settings popup routes added 2026-11-10: account, schedule, engine, infrastructure.
    assert routes == {"/api/overview", "/api/limits", "/api/setup", "/api/portfolio",
                      "/api/trading-mode", "/api/loop/start", "/api/loop/stop",
                      "/api/account", "/api/account/login", "/api/account/logout",
                      "/api/engine", "/api/engine/start", "/api/engine/stop", "/api/infrastructure", "/api/schedule"}
    # saveLimits, saveSetup, postMode, postLoop, plus the jpost helper for settings writes
    assert text.count('method: "POST"') == 5
    assert '"X-Dashboard-Token"' in text


def test_page_has_no_kill_switch_controls():
    # Owner request 2026-11-09: kill switch removed from the page.
    text = PAGE.read_text(encoding="utf-8")
    for element_id in ("killtrip", "killreset", "killreason", "killmsg", "headkill", "killbanner", "r-kill"):
        assert f'id="{element_id}"' not in text, element_id
    assert 'id="token"' in text


def test_page_never_stores_the_token():
    text = PAGE.read_text(encoding="utf-8")
    for store in ("localStorage", "sessionStorage", "document.cookie"):
        assert store not in text, store


def test_page_has_an_input_for_every_risk_limit():
    from dataclasses import fields

    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from quant.core.config import RiskLimits

    text = PAGE.read_text(encoding="utf-8")
    covered = set(re.findall(r'data-limit="(\w+)"', text))
    assert covered == {f.name for f in fields(RiskLimits)}


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
