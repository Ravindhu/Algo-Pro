"""Main dashboard (decision 0027): dashboard/main_dashboard.html is what /ui serves.

Route, panel and safety checks for the page are in test_pnl_visible.py.
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


def test_page_never_stores_the_token():
    text = PAGE.read_text(encoding="utf-8")
    for store in ("localStorage", "sessionStorage", "document.cookie"):
        assert store not in text, store


def test_page_has_no_order_code_or_simulated_random():
    text = PAGE.read_text(encoding="utf-8")
    for banned in ("placeorder", "Math.random"):
        assert banned not in text, banned


def test_paper_loop_controls_live_in_the_paper_trading_panel():
    text = PAGE.read_text(encoding="utf-8")
    header = text[text.index("<header>"):text.index("</header>")]
    panel = text[text.index('id="sbmodal"'):text.index('id="pfmodal"')]
    for element_id in ("loopstart", "sbstop", "loopmsg"):
        assert f'id="{element_id}"' in panel, element_id
        assert f'id="{element_id}"' not in header, element_id
    assert '"/api/loop/start"' in text and '"/api/loop/stop"' in text


def test_header_has_engine_start_only_and_kill_stop_reset():
    text = PAGE.read_text(encoding="utf-8")
    header = text[text.index("<header>"):text.index("</header>")]
    for element_id in ("engstart", "killstop", "killreset", "killmsg"):
        assert f'id="{element_id}"' in header, element_id
    for element_id in ("engstop", "killchip"):
        assert f'id="{element_id}"' not in text, element_id
    assert '"/api/kill/trip"' in text and '"/api/kill/reset"' in text
    assert "window.prompt" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_main_dashboard_script_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)
    assert scripts
    for i, sc in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(sc, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_flow_popup_uses_the_v5_svg_asset():
    text = PAGE.read_text(encoding="utf-8")
    assert '<script src="/assets/flow_svg.js"></script>' in text
    assert 'id="flowbox"' in text and "window.FLOW_SVG" in text
    assert "buildFlow" not in text  # the hand-drawn copy is gone
