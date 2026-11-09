"""Live checklist (decision 0031). Each check is tested alone. Hand-checked: 2026-11-10 is an NSE
equity holiday (Diwali Balipratipada). No test places an order."""
import dataclasses
import pathlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quant.core.live_checklist import LiveFacts, allowed, evaluate
from quant.core.live_facts import gather

REPO = pathlib.Path(__file__).resolve().parents[3]
IST = ZoneInfo("Asia/Kolkata")


def _facts(**kw):
    base = dict(calendar_verified=True, is_trading_day=True,
                now_ist=datetime(2026, 11, 9, 11, 0, tzinfo=IST),
                data_source_licensed=True, approver="Owner",
                approval_limits_sha256="abc", current_limits_sha256="abc",
                max_symbols_per_portfolio=8, openalgo_mode="live", kill_active=False,
                safe_mode_halt=False, daily_loss_breach=False, recon_clean=True)
    base.update(kw)
    return LiveFacts(**base)


def _failed(facts):
    return {c.name for c in evaluate(facts) if not c.passed}


def test_all_good_facts_pass_all_eight_checks():
    checks = evaluate(_facts())
    assert len(checks) == 8 and allowed(checks)


@pytest.mark.parametrize("change, failing", [
    ({"calendar_verified": False}, "calendar"),
    ({"data_source_licensed": False}, "licensed_data"),
    ({"data_source_licensed": None}, "licensed_data"),
    ({"approver": None}, "approval"),
    ({"approver": "   "}, "approval"),
    ({"current_limits_sha256": "changed"}, "approval"),
    ({"max_symbols_per_portfolio": None}, "max_symbols_set"),
    ({"openalgo_mode": "analyze"}, "openalgo"),
    ({"openalgo_mode": None}, "openalgo"),
    ({"kill_active": True}, "kill_safe_loss"),
    ({"safe_mode_halt": None}, "kill_safe_loss"),
    ({"daily_loss_breach": True}, "kill_safe_loss"),
    ({"recon_clean": False}, "reconciliation"),
    ({"recon_clean": None}, "reconciliation"),
])
def test_each_check_fails_on_its_own_fact(change, failing):
    assert _failed(_facts(**change)) == {failing}


@pytest.mark.parametrize("hhmm, ok", [("09:14", False), ("09:15", True), ("15:29", True), ("15:30", False)])
def test_session_window_is_915_to_1530(hhmm, ok):
    h, m = map(int, hhmm.split(":"))
    f = _facts(now_ist=datetime(2026, 11, 9, h, m, tzinfo=IST))
    assert ("session" not in _failed(f)) is ok


def test_non_trading_day_fails_calendar_and_session():
    f = _facts(is_trading_day=False)
    assert {"calendar", "session"} == _failed(f)


def test_holiday_2026_11_10_refused_all_day():
    f = _facts(now_ist=datetime(2026, 11, 10, 11, 0, tzinfo=IST), is_trading_day=False)
    assert {"calendar", "session"} <= _failed(f)


def test_unknown_facts_fail_closed():
    f = _facts(openalgo_mode=None, safe_mode_halt=None, daily_loss_breach=None, recon_clean=None)
    assert not allowed(evaluate(f))


def test_repo_state_keeps_live_refused_until_licence_is_approved(tmp_path):
    """Regression guard: with today's repo files, live cannot pass (no licensed live source yet)."""
    f = gather(REPO, tmp_path / "kill.json", "Owner", datetime(2026, 11, 9, 11, 0, tzinfo=IST))
    assert f.data_source_licensed is False
    assert f.max_symbols_per_portfolio == 8
    assert "licensed_data" in _failed(f)
    assert not allowed(evaluate(f))
