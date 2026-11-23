"""
test_login_guard.py - the rate cap that stops repeated broker logins.

WHY THIS FILE EXISTS
====================
On 2026-10-27 repeated login attempts during testing made OpenAlgo's login hook
slow enough to stall, and every attempt still reached the broker. A broker locks
an account for that. OpenAlgo itself rate-limits ``/auth/login`` to 5/minute and
25/hour per IP, so "just retry" is not a safe recovery strategy.

These tests pin the policy without ever contacting a broker:

1. No record -> allowed (nothing has been spent yet).
2. Inside the post-success window -> refused, with the wait time reported.
3. A FAILED attempt waits LONGER than a successful one.
4. Once the window elapses -> allowed again.
5. An unreadable guard file refuses rather than failing open into a login.
6. The gate is wired into ``scheduled_login.py`` and ``launch_login`` - a guard
   nothing calls is worse than no guard, because it looks safe.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyotp")

from blue_engine.core import broker_auth  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]  # tests -> blue_engine -> quant -> src -> repo


@pytest.fixture()
def guard_path(tmp_path, monkeypatch):
    """Point the guard at a temp file so no real run state is touched."""
    target = tmp_path / "login_guard.json"
    monkeypatch.setattr(broker_auth, "_login_state_path", lambda: target)
    return target


def _write(path: Path, *, epoch: float, ok: bool) -> None:
    path.write_text(json.dumps({
        "last_attempt_epoch": epoch,
        "last_attempt_at": "2026-10-27T00:00:00+00:00",
        "last_ok": ok,
        "outcome": "test",
    }), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_no_record_means_allowed(guard_path):
    out = broker_auth.login_guard(now=1_000_000.0)
    assert out["allowed"] is True
    assert out["reason"] == "no login attempt on record"


def test_a_success_blocks_a_retry_inside_the_window(guard_path):
    _write(guard_path, epoch=1000.0, ok=True)
    window = broker_auth.LOGIN_MIN_INTERVAL_SECONDS
    out = broker_auth.login_guard(now=1000.0 + window - 5)
    assert out["allowed"] is False
    assert out["wait_seconds"] == pytest.approx(5.0)
    assert "lock" in out["reason"].lower()


def test_a_failure_waits_longer_than_a_success(guard_path):
    assert (broker_auth.LOGIN_FAILURE_BACKOFF_SECONDS
            > broker_auth.LOGIN_MIN_INTERVAL_SECONDS), \
        "a failed attempt must back off harder than a successful one"
    _write(guard_path, epoch=1000.0, ok=False)
    # Past the success window, still inside the failure window.
    inside = broker_auth.login_guard(
        now=1000.0 + broker_auth.LOGIN_MIN_INTERVAL_SECONDS + 1)
    assert inside["allowed"] is False, "a failure must not be retried that soon"


def test_the_window_elapses_and_allows_again(guard_path):
    _write(guard_path, epoch=1000.0, ok=False)
    out = broker_auth.login_guard(
        now=1000.0 + broker_auth.LOGIN_FAILURE_BACKOFF_SECONDS + 1)
    assert out["allowed"] is True
    assert out["reason"] == "cooldown elapsed"


def test_an_unreadable_guard_refuses_instead_of_failing_open(guard_path):
    guard_path.write_text("{not json", encoding="utf-8")
    out = broker_auth.login_guard(now=1_000_000.0)
    assert out["allowed"] is False, (
        "a corrupt guard must not be read as 'no attempt made' - that would "
        "let a retry storm through")
    assert "unreadable" in out["reason"]


def test_recording_an_attempt_then_guarding_blocks_the_next(guard_path):
    """End to end, in the same second: a second login must be refused."""
    assert guard_path is not None
    broker_auth.record_login_attempt(ok=True, outcome="test success")
    assert guard_path.is_file(), "the attempt must be stamped to disk"
    out = broker_auth.login_guard()
    assert out["allowed"] is False


def test_the_recorded_outcome_is_not_a_secret(guard_path):
    """Callers pass a reason; this test pins that no TOTP-shaped value leaks."""
    broker_auth.record_login_attempt(ok=False, outcome="broker TOTP login "
                                     "failed (HTTP 429): rate limited")
    blob = guard_path.read_text(encoding="utf-8")
    assert "429" in blob
    assert not any(tok.isdigit() and len(tok) == 6 for tok in blob.split())


# --------------------------------------------------------------------------- #
# Wiring - a guard nothing calls protects nothing
# --------------------------------------------------------------------------- #
def test_scheduled_login_refuses_on_cooldown_before_touching_the_broker(
        guard_path, monkeypatch):
    """run_scheduled_login must return cooldown WITHOUT running BrokerAuth."""
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k):  # noqa: D401
            called["n"] += 1
            raise AssertionError("BrokerAuth must not be constructed on cooldown")

    monkeypatch.setattr(broker_auth, "BrokerAuth", _Boom)
    monkeypatch.setattr(broker_auth, "autologin_enabled", lambda: True)
    _write(guard_path, epoch=broker_auth.time.time(), ok=False)

    out = broker_auth.run_scheduled_login(enable=True)
    assert out["ok"] is False
    assert out.get("cooldown") is True
    assert called["n"] == 0


def test_the_cli_script_calls_the_guard():
    text = (ROOT / "scripts" / "ops" / "scheduled_login.py").read_text(
        encoding="utf-8")
    assert "login_guard()" in text, "the script must check the guard"
    assert "record_login_attempt" in text, "the script must stamp attempts"
    assert "EXIT_COOLDOWN" in text


def test_no_dashboard_can_login_around_the_guard():
    """No dashboard may expose a broker-login path that skips ``login_guard``.

    HISTORY: this test used to read
    ``Blue_engine/dashboard/ui/service_control.py`` and assert that the LOGIN
    button called ``login_guard``. That terminal console was deleted on
    2026-11-05 (commit 1d72420, "remove Blue terminal console; FastAPI dashboard
    replaces it"), so the test failed with FileNotFoundError while the property
    it was protecting still held -- the new dashboard simply has no login path.

    The invariant is restated so it survives the next dashboard rewrite: scan
    every dashboard module. Any module that can start a broker login must also
    reference the guard. A module with no login path passes trivially, which is
    the correct outcome (nothing to bypass).
    """
    dashboards = [ROOT / "dashboard", ROOT / "src" / "quant" / "blue_engine" / "dashboard"]
    login_markers = ("launch_login", "run_scheduled_login", "BrokerAuth(")
    checked = 0
    for folder in dashboards:
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            checked += 1
            if any(marker in text for marker in login_markers):
                assert "login_guard" in text, (
                    f"{path.relative_to(ROOT)} can log in but never calls "
                    "login_guard(); that is a path around the rate cap")
    assert checked > 0, "no dashboard module was found - the scan is vacuous"


def test_the_shipped_task_retries_once_not_three_times():
    """The shipped template must be IMPORTABLE and must not retry in a storm."""
    import xml.etree.ElementTree as ET

    raw = (ROOT / "scripts" / "ops" / "broker_login_task.xml").read_bytes()
    # Task Scheduler requires UTF-16; the declaration must match the bytes.
    assert raw[:2] == b"\xff\xfe", "the file must be UTF-16LE with a BOM"
    text = raw.decode("utf-16")
    root = ET.fromstring(text)  # raises if the XML is malformed

    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    restart = root.find("t:Settings/t:RestartOnFailure", ns)
    assert restart is not None, "RestartOnFailure must exist"
    assert restart.findtext("t:Count", namespaces=ns) == "1", \
        "3 retries in 5 minutes is how an account gets locked"
    # A retry sooner than the script's own cooldown would just exit 8.
    interval = restart.findtext("t:Interval", namespaces=ns)
    assert interval == "PT15M", interval
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT10M"
