"""P&L shown by default on the web dashboard; a toggle can hide it (decision 0007).

The Blue terminal console was removed 2026-11-05, so only the web page (dashboard/index.html) is checked here.
Web tests are text checks plus a Node syntax check, so they run without extra packages.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HTML = ROOT / "dashboard" / "index.html"


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
