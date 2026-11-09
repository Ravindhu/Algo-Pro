"""Trading mode (decisions 0028 + 0030): paper always works; live needs an approval.

Supersedes the decision-0028 version of this file, which asserted that live is
ALWAYS refused. Decision 0030 keeps live locked but makes the lock an approval
file instead of an unconditional refusal, so the pass/block expectations changed.
The deep guard tests live in src/quant/tests/test_trading_mode_live.py; these
cover the contract the wider suite imports.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.trading_mode import (  # noqa: E402
    MODES, request_mode, trading_mode_status,
)


def test_modes_are_paper_and_live():
    assert set(MODES) == {"paper", "live"}


def test_a_fresh_install_reports_paper(tmp_path):
    """No mode file anywhere: the honest report is paper."""
    s = trading_mode_status(state_path=tmp_path / "missing.json")
    assert s["mode"] == "paper"
    assert s["effective_mode"] == "paper"
    assert s["live_allowed"] is False


def test_paper_is_accepted(tmp_path):
    ok, msg = request_mode("paper", state_path=tmp_path / "mode.json",
                           approval_path=tmp_path / "a.json",
                           limits_path=tmp_path / "l.yaml")
    assert ok is True and msg == "Paper mode selected."


def test_live_is_refused_without_an_approval(tmp_path):
    ok, msg = request_mode("live", state_path=tmp_path / "mode.json",
                           approval_path=tmp_path / "nope.json",
                           limits_path=tmp_path / "l.yaml")
    assert ok is False
    assert "approval" in msg


def test_live_mode_is_not_created_by_a_refusal(tmp_path):
    state = tmp_path / "mode.json"
    request_mode("live", state_path=state, approval_path=tmp_path / "nope.json",
                 limits_path=tmp_path / "l.yaml")
    assert not state.exists()


def test_invalid_mode_is_refused(tmp_path):
    state = tmp_path / "mode.json"
    for bad in (None, "", "PAPER ", ["live"], "analyze"):
        accepted, _ = request_mode(bad, state_path=state,
                                   approval_path=tmp_path / "nope.json",
                                   limits_path=tmp_path / "l.yaml")
        assert accepted is False, bad
    assert not state.exists()
