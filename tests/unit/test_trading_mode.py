"""Trading mode (decision 0028): paper is selectable, live is always refused."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.trading_mode import LIVE_REFUSAL, request_mode, trading_mode_status  # noqa: E402


def test_status_is_paper_and_live_not_allowed():
    s = trading_mode_status()
    assert s["mode"] == "paper"
    assert s["live_allowed"] is False
    assert s["reason"] == LIVE_REFUSAL


def test_paper_is_accepted():
    assert request_mode("paper") == (True, "Paper mode selected.")


def test_live_is_always_refused():
    accepted, message = request_mode("live")
    assert accepted is False
    assert "decision 0017" in message


def test_invalid_mode_is_refused():
    for bad in (None, "", "PAPER ", ["live"], "analyze"):
        accepted, _ = request_mode(bad)
        assert accepted is False, bad
