"""Trading mode (decisions 0028 and 0031): PAPER always; LIVE only with the confirm phrase,
a named approver and every live check passing. Restart returns to PAPER."""
import importlib
import pathlib
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core import trading_mode as tm  # noqa: E402
from quant.core.live_checklist import Check  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _passing():
    return [Check(n, True, "ok") for n in ("calendar", "session", "licensed_data", "approval",
                                           "max_symbols_set", "openalgo", "kill_safe_loss", "reconciliation")]


def _reset():
    tm._current = "paper"


def test_status_reports_mode_and_what_live_needs():
    s = tm.trading_mode_status()
    assert s["mode"] == "paper" and s["confirm_phrase"] == "GO LIVE"
    assert len(s["live_needs"]) == 3


def test_paper_is_accepted_and_stays_paper():
    _reset()
    assert tm.request_mode("paper") == (True, "Paper mode selected.")
    assert tm.current_mode() == "paper"


def test_live_refused_without_checklist():
    _reset()
    accepted, message = tm.request_mode("live", confirm="GO LIVE", approver="Owner")
    assert accepted is False and "checklist was not run" in message
    assert tm.current_mode() == "paper"


def test_live_refused_without_exact_confirm_phrase():
    _reset()
    for bad in (None, "go live", "GO LIVE ", "YES"):
        accepted, _ = tm.request_mode("live", checks=_passing(), confirm=bad, approver="Owner")
        assert accepted is False, bad
    assert tm.current_mode() == "paper"


def test_live_refused_without_named_approver():
    _reset()
    for bad in (None, "", "   "):
        accepted, message = tm.request_mode("live", checks=_passing(), confirm="GO LIVE", approver=bad)
        assert accepted is False and "approver" in message
    assert tm.current_mode() == "paper"


def test_live_refused_names_each_failing_check():
    _reset()
    checks = _passing()
    checks[2] = Check("licensed_data", False, "no licensed live data source")
    accepted, message = tm.request_mode("live", checks=checks, confirm="GO LIVE", approver="Owner")
    assert accepted is False
    assert "licensed_data: no licensed live data source" in message
    assert tm.current_mode() == "paper"


def test_live_accepted_only_when_everything_passes():
    _reset()
    accepted, message = tm.request_mode("live", checks=_passing(), confirm="GO LIVE", approver="Owner")
    assert accepted is True and message == "Live mode selected."
    assert tm.current_mode() == "live"
    tm.request_mode("paper")
    assert tm.current_mode() == "paper"


def test_invalid_mode_is_refused():
    for bad in (None, "", "PAPER ", ["live"], "analyze"):
        accepted, _ = tm.request_mode(bad)
        assert accepted is False, bad


def test_restart_returns_to_paper():
    tm._current = "live"
    importlib.reload(tm)
    assert tm.current_mode() == "paper"


def test_only_trading_mode_writes_the_mode():
    """The mode may be changed only inside core/trading_mode.py (decision 0031, the one switch)."""
    offenders = []
    for base in ("src/quant", "dashboard", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            if "vendor" in path.parts or "openalgo_platform" in path.parts or "tests" in path.parts:
                continue
            if path.name == "trading_mode.py" and path.parent.name == "core":
                continue  # the one place allowed to write the mode
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "trading_mode._current" in text or "_current = \"live\"" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders
