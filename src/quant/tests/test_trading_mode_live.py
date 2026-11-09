"""Trading mode: live is selectable ONLY with a valid approval (decision 0030).

Flow stage: Dashboard
Status: TEST

The dashboard switch is a request, never authority. These tests pin the two
directions of that: paper always works, and live works exactly when a person's
approval is valid - not when a flag is flipped, not when a stale approval lingers.
"""
import json
from datetime import date

from quant.core.live_approval import APPROVAL_PHRASE, create_approval
from quant.core.trading_mode import request_mode, trading_mode_status

LIMITS = "max_order_quantity: 10\nmax_daily_loss: 1000\n"


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


def _setup(tmp_path, limits_text=LIMITS):
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text(limits_text, encoding="utf-8")
    state = tmp_path / "mode.json"
    approval = tmp_path / "live_approval.json"
    return state, approval, limits


def _approve(approval, limits):
    create_approval(approval, approver="Owner", reason="test",
                    limits_path=limits, phrase=APPROVAL_PHRASE)


# --------------------------------------------------------------------------- #
# PASS paths
# --------------------------------------------------------------------------- #
def test_paper_is_always_selectable(tmp_path):
    state, approval, limits = _setup(tmp_path)
    ok, msg = request_mode("paper", state_path=state, approval_path=approval,
                           limits_path=limits)
    assert ok is True
    assert json.loads(state.read_text())["mode"] == "paper"


def test_live_is_accepted_with_a_valid_approval(tmp_path):
    state, approval, limits = _setup(tmp_path)
    _approve(approval, limits)
    ok, msg = request_mode("live", state_path=state, approval_path=approval,
                           limits_path=limits)
    assert ok is True
    assert json.loads(state.read_text())["mode"] == "live"
    st = trading_mode_status(state_path=state, approval_path=approval, limits_path=limits)
    assert st["mode"] == "live" and st["effective_mode"] == "live"
    assert st["live_allowed"] is True and st["approver"] == "Owner"


def test_the_status_reports_the_requested_mode_separately(tmp_path):
    """effective_mode is what is actually armed, not what was asked for."""
    state, approval, limits = _setup(tmp_path)
    _approve(approval, limits)
    request_mode("live", state_path=state, approval_path=approval, limits_path=limits)
    # the limits change: the approval is void
    limits.write_text(LIMITS.replace("10", "11"), encoding="utf-8")
    st = trading_mode_status(state_path=state, approval_path=approval, limits_path=limits)
    assert st["mode"] == "live", "the REQUEST is still live"
    assert st["effective_mode"] == "paper", "what is ARMED is paper"
    assert st["live_allowed"] is False


# --------------------------------------------------------------------------- #
# BLOCK paths
# --------------------------------------------------------------------------- #
def test_live_without_an_approval_is_refused(tmp_path):
    state, approval, limits = _setup(tmp_path)
    ok, msg = request_mode("live", state_path=state, approval_path=approval,
                           limits_path=limits)
    assert ok is False
    assert "approve_live.py" in msg
    assert not state.exists(), "a refused request must not write state"


def test_live_with_a_voided_approval_is_refused(tmp_path):
    state, approval, limits = _setup(tmp_path)
    _approve(approval, limits)
    limits.write_text(LIMITS.replace("10", "12"), encoding="utf-8")
    ok, msg = request_mode("live", state_path=state, approval_path=approval,
                           limits_path=limits)
    assert ok is False
    assert "void" in msg


def test_an_invalid_mode_is_refused_and_never_writes_state(tmp_path):
    state, approval, limits = _setup(tmp_path)
    for bad in (None, "", "LIVE", "both", 1):
        ok, msg = request_mode(bad, state_path=state, approval_path=approval,
                               limits_path=limits)
        assert ok is False
    assert not state.exists()


def test_every_refusal_is_audited(tmp_path):
    state, approval, limits = _setup(tmp_path)
    audit = _Audit()
    request_mode("live", state_path=state, approval_path=approval,
                 limits_path=limits, audit=audit)
    request_mode("banana", state_path=state, approval_path=approval,
                 limits_path=limits, audit=audit)
    kinds = [k for k, _ in audit.rows]
    assert kinds.count("mode_change_refused") == 2


def test_the_acceptance_is_audited_too(tmp_path):
    state, approval, limits = _setup(tmp_path)
    _approve(approval, limits)
    audit = _Audit()
    request_mode("live", state_path=state, approval_path=approval,
                 limits_path=limits, audit=audit)
    kinds = [k for k, _ in audit.rows]
    assert "mode_changed" in kinds


def test_a_missing_or_corrupt_state_file_means_paper(tmp_path):
    """A file that cannot be read must never look like live."""
    state, approval, limits = _setup(tmp_path)
    state.write_text("{broken", encoding="utf-8")
    st = trading_mode_status(state_path=state, approval_path=approval, limits_path=limits)
    assert st["mode"] == "paper" and st["effective_mode"] == "paper"


def test_paper_after_live_always_works_without_approval(tmp_path):
    state, approval, limits = _setup(tmp_path)
    _approve(approval, limits)
    request_mode("live", state_path=state, approval_path=approval, limits_path=limits)
    ok, msg = request_mode("paper", state_path=state, approval_path=approval,
                           limits_path=limits)
    assert ok is True
    assert json.loads(state.read_text())["mode"] == "paper"
