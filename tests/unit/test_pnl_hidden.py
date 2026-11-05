"""P&L shown by default on both dashboards; a toggle can hide it (decision 0007).

Terminal tests need ``rich`` and ``Blue_engine``. They skip when those are missing.
Web tests are text checks plus a Node syntax check, so they run without extra packages.
"""
import io
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HTML = ROOT / "dashboard" / "index.html"


# --- terminal dashboard ----------------------------------------------------

def _render_module():
    pytest.importorskip("rich")
    from quant.core.blue_bridge import load
    return load("dashboard.render")


def _to_text(renderable) -> str:
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=220, record=True, color_system=None).print(renderable)
    return buf.getvalue()


ROW = {
    "symbol": "RELIANCE", "exchange": "NSE", "ltp": 2500.0, "change_pct": 1.2,
    "position": 10, "avg_price": 2400.0, "unrealized_pnl": 1234.0, "state": "HOLDING",
}


def test_universe_table_shows_pnl_by_default():
    mod = _render_module()
    text = _to_text(mod.render_universe_table([ROW]))
    assert "+1,234" in text


def test_universe_table_hides_pnl_when_turned_off():
    mod = _render_module()
    text = _to_text(mod.render_universe_table([ROW], reveal_pnl=False))
    assert "hidden" in text
    assert "+1,234" not in text


def test_universe_table_shows_pnl_when_revealed():
    mod = _render_module()
    text = _to_text(mod.render_universe_table([ROW], reveal_pnl=True))
    assert "+1,234" in text


def test_detail_panel_shows_pnl_by_default():
    mod = _render_module()
    text = _to_text(mod.render_detail_panel(ROW, []))
    assert "Unrealized P&L" in text
    assert "+1,234" in text


def test_detail_panel_hides_pnl_when_turned_off():
    mod = _render_module()
    text = _to_text(mod.render_detail_panel(ROW, [], reveal_pnl=False))
    assert "hidden" in text
    assert "+1,234" not in text


def test_detail_panel_shows_pnl_when_revealed():
    mod = _render_module()
    text = _to_text(mod.render_detail_panel(ROW, [], reveal_pnl=True))
    assert "+1,234" in text


def test_position_size_stays_visible_when_pnl_hidden():
    mod = _render_module()
    text = _to_text(mod.render_detail_panel(ROW, []))
    assert "10 @ 2,400.00" in text


# --- web dashboard ----------------------------------------------------------

def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def test_web_daily_pnl_shown_by_default():
    s = _html()
    assert "let revealPnl=true" in s
    assert "revealPnl?money(day,true):'••••'" in s
    assert 'id="rev" type="button">Hide ₹<' in s


def test_web_equity_and_total_gated_by_flag():
    s = _html()
    assert "setT('eqv',revealPnl?money(eq):'••••')" in s
    assert "setT('totp',revealPnl?money(tot,true):'••••'" in s
    assert "setT('tr',revealPnl?" in s


def test_web_money_never_written_without_reveal_check():
    # Every money write in ui() must sit behind the reveal flag.
    s = _html()
    for match in re.finditer(r"setT\('(eqv|totp|tr)',([^)]*)", s):
        assert "revealPnl" in match.group(0), match.group(0)


def test_web_has_show_toggle():
    s = _html()
    assert 'id="rev"' in s
    assert "revealPnl=!revealPnl" in s


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_script_parses(tmp_path):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", _html(), re.S)
    assert scripts
    for i, sc in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(sc, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
