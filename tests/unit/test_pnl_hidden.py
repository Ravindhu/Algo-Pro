"""P&L shown by default (decision 0007). The flow page is a static diagram (decision 0020).

The old simulated dashboard page (dashboard/index.html) was moved out of the served app.
Its simulated order engine is removed; the file now lives at src/quant/index.html.
Web tests are text checks plus a Node syntax check, so they run without extra packages.
"""
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGE = ROOT / "src" / "quant" / "index.html"
FLOW_JS = ROOT / "dashboard" / "assets" / "flow_svg.js"


def test_pnl_visible_by_default_in_settings():
    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert data["dashboard"]["pnl_visible_default"] is True


def test_old_simulated_page_is_gone():
    assert not (ROOT / "dashboard" / "index.html").exists()


def test_page_has_no_order_code_or_simulated_data():
    text = PAGE.read_text(encoding="utf-8")
    for banned in ("placeorder", "Math.random", "open.push"):
        assert banned not in text, banned


def test_page_loads_the_flow_asset_from_the_app():
    text = PAGE.read_text(encoding="utf-8")
    assert '<script src="/assets/flow_svg.js">' in text
    assert FLOW_JS.exists()


def test_page_reads_overview_and_writes_only_known_routes():
    text = PAGE.read_text(encoding="utf-8")
    assert 'fetch("/api/overview"' in text
    routes = set(re.findall(r'"(/api/[\w/]+)"', text))
    assert routes == {"/api/overview", "/api/limits", "/api/kill/trip", "/api/kill/reset"}
    assert text.count('method: "POST"') == 2  # saveLimits and postKill only
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
