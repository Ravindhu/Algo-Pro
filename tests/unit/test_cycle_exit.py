"""Exit rules for cycle positions (rules doc 11.9): no fixed target, no breakeven, ATR trail from entry."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.execution.exit_manager import ExitConfig, Position, evaluate_bar  # noqa: E402

CFG = ExitConfig(breakeven_r=1.0, trail_mult=2.0)


def _trail_position():
    return Position(key="k", symbol="X", side="BUY", qty=10, entry=10.1, stop=9.0, target=None,
                    atr=0.5, breakeven_enabled=False, trail_from_start=True, strategy="engulfing")


def test_no_fixed_target_means_no_target_exit():
    pos = Position(key="k", symbol="X", side="BUY", qty=10, entry=11.0, stop=9.0, target=None, atr=0.5,
                   breakeven_enabled=False)
    exit_px, reason = evaluate_bar(pos, bar_open=20.0, bar_high=30.0, bar_low=19.0, cfg=CFG)
    assert exit_px is None and reason == ""


def test_stop_still_exits_when_no_target():
    pos = Position(key="k", symbol="X", side="BUY", qty=10, entry=11.0, stop=9.0, target=None, atr=0.5,
                   breakeven_enabled=False)
    exit_px, reason = evaluate_bar(pos, bar_open=9.5, bar_high=9.8, bar_low=8.9, cfg=CFG)
    assert reason == "stop" and exit_px == 9.0


def test_breakeven_disabled_keeps_stop_at_htf_low():
    pos = Position(key="k", symbol="X", side="BUY", qty=10, entry=11.0, stop=9.0, target=None, atr=0.5,
                   breakeven_enabled=False)
    evaluate_bar(pos, bar_open=12.0, bar_high=15.0, bar_low=11.8, cfg=CFG)
    assert pos.be_done is False and pos.stop == 9.0


def test_trail_from_start_tightens_stop_after_price_rises():
    pos = _trail_position()
    evaluate_bar(pos, bar_open=10.2, bar_high=12.0, bar_low=10.1, cfg=CFG)
    # best 12.0, trail = 12.0 - 2 x 0.5 = 11.0 -> stop moves up from 9.0 to 11.0
    assert pos.stop == 11.0


def test_trail_only_tightens_from_the_htf_low_floor():
    pos = _trail_position()
    evaluate_bar(pos, bar_open=10.2, bar_high=10.3, bar_low=10.1, cfg=CFG)
    # trail = 10.3 - 2 x 0.5 = 9.3, above the HTF low 9.0, so the stop tightens to 9.3
    assert pos.stop == 9.3
    evaluate_bar(pos, bar_open=10.0, bar_high=10.0, bar_low=9.9, cfg=CFG)
    assert pos.stop == 9.3  # a lower high never loosens the stop
