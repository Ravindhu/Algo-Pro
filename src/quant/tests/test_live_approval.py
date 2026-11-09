"""LiveApproval: the human sign-off that arms live mode (decision 0030).

Flow stage: Risk gate
Status: TEST

The approval must be possible to create only by a person who typed a phrase,
and impossible to keep valid after the limits change.
"""
import json
from datetime import date, timedelta

import pytest

from quant.core.live_approval import (
    APPROVAL_PHRASE,
    MAX_AGE_DAYS,
    check_approval,
    create_approval,
    file_sha256,
    load_approval,
    revoke_approval,
)

LIMITS = "max_order_quantity: 10\nmax_daily_loss: 1000\n"


@pytest.fixture
def limits(tmp_path):
    p = tmp_path / "risk_limits.yaml"
    p.write_text(LIMITS, encoding="utf-8")
    return p


def _approve(path, limits, **over):
    kw = dict(approver="Owner", reason="paper checks passed",
              limits_path=limits, phrase=APPROVAL_PHRASE)
    kw.update(over)
    return create_approval(path, **kw)


# --------------------------------------------------------------------------- #
# PASS path
# --------------------------------------------------------------------------- #
def test_a_person_can_create_an_approval(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    rec = _approve(out, limits)
    assert rec.approver == "Owner"
    assert rec.limits_sha256 == file_sha256(limits)
    assert load_approval(out).approver == "Owner"


def test_a_fresh_approval_checks_valid(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    _approve(out, limits)
    ok, why = check_approval(out, limits, today=date(2026, 11, 8))
    assert ok is True
    assert "Owner" in why


# --------------------------------------------------------------------------- #
# BLOCK paths - one per rule
# --------------------------------------------------------------------------- #
def test_a_wrong_phrase_creates_nothing(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    for bad in ("", "yes", "i approve live trading", APPROVAL_PHRASE.lower(),
                APPROVAL_PHRASE + " "):
        with pytest.raises(ValueError):
            _approve(out, limits, phrase=bad)
    assert not out.exists(), "a refused approval must not leave a file behind"


def test_a_blank_approver_or_reason_is_refused(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    with pytest.raises(ValueError):
        _approve(out, limits, approver="  ")
    with pytest.raises(ValueError):
        _approve(out, limits, reason="")
    assert not out.exists()


def test_a_missing_limits_file_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _approve(tmp_path / "a.json", tmp_path / "nope.yaml")


def test_changed_limits_void_the_approval(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    _approve(out, limits)
    limits.write_text(LIMITS.replace("max_order_quantity: 10", "max_order_quantity: 100"),
                      encoding="utf-8")
    ok, why = check_approval(out, limits, today=date(2026, 11, 8))
    assert ok is False
    assert "void" in why


def test_a_missing_limits_file_fails_the_check(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    _approve(out, limits)
    ok, why = check_approval(out, tmp_path / "gone.yaml", today=date(2026, 11, 8))
    assert ok is False


def test_an_old_approval_is_stale(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    _approve(out, limits)          # approved 2026-11-08
    later = date(2026, 11, 8) + timedelta(days=MAX_AGE_DAYS + 1)
    ok, why = check_approval(out, limits, today=later)
    assert ok is False
    assert "re-approve" in why


def test_a_future_dated_approval_is_refused(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    _approve(out, limits, today=date(2026, 12, 31))
    ok, why = check_approval(out, limits, today=date(2026, 11, 8))
    assert ok is False


def test_a_corrupt_approval_file_is_not_an_approval(tmp_path):
    p = tmp_path / "live_approval.json"
    p.write_text("{not json", encoding="utf-8")
    ok, why = check_approval(p, tmp_path / "l.yaml", today=date(2026, 11, 8))
    assert ok is False
    assert "no live approval" in why


def test_a_tampered_approver_is_detected(tmp_path, limits):
    out = tmp_path / "live_approval.json"
    _approve(out, limits)
    data = json.loads(out.read_text(encoding="utf-8"))
    data["approver"] = ""          # someone scrubbed the name
    out.write_text(json.dumps(data), encoding="utf-8")
    ok, why = check_approval(out, limits, today=date(2026, 11, 8))
    assert ok is False
    assert "no approver" in why


def test_revoke_removes_the_file(limits, tmp_path):
    out = tmp_path / "live_approval.json"
    _approve(out, limits)
    assert revoke_approval(out) is True
    assert revoke_approval(out) is False
    assert load_approval(out) is None
