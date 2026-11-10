"""Flow stage: Protection (exit rules). Status: TESTS for PHASE 3 IMPLEMENTED exit_manager.
Pure rule tests: stop, target, gap fills, breakeven, ATR trail, and the SELL mirror.
"""
import pytest

from quant.execution.exit_manager import ExitConfig, Position, evaluate_bar

CFG = ExitConfig(breakeven_r=1.0, trail_mult=2.0)


def _long():
    # entry 100, stop 98 -> R = 2, target 106, ATR 1
    return Position(key="k", symbol="INFY", side="BUY", qty=100, entry=100.0,
                    stop=98.0, target=106.0, atr=1.0)


def _short():
    return Position(key="k", symbol="INFY", side="SELL", qty=100, entry=100.0,
                    stop=102.0, target=94.0, atr=1.0)


# --- BUY ---

def test_long_stop_hit_exits_at_stop():
    pos = _long()
    price, reason = evaluate_bar(pos, bar_open=99.0, bar_high=99.5, bar_low=97.5, cfg=CFG)
    assert (price, reason) == (98.0, "stop")


def test_long_gap_below_stop_exits_at_open():
    pos = _long()
    price, reason = evaluate_bar(pos, bar_open=96.0, bar_high=96.5, bar_low=95.0, cfg=CFG)
    assert (price, reason) == (96.0, "stop")


def test_long_target_hit_exits_at_target():
    pos = _long()
    price, reason = evaluate_bar(pos, bar_open=101.0, bar_high=107.0, bar_low=100.0, cfg=CFG)
    assert (price, reason) == (106.0, "target")


def test_long_same_bar_stop_and_target_stop_wins():
    pos = _long()
    price, reason = evaluate_bar(pos, bar_open=100.0, bar_high=107.0, bar_low=97.0, cfg=CFG)
    assert (price, reason) == (98.0, "stop")


def test_long_breakeven_moves_stop_to_entry():
    pos = _long()
    price, _ = evaluate_bar(pos, bar_open=100.0, bar_high=102.5, bar_low=100.0, cfg=CFG)
    assert price is None
    assert pos.be_done is True
    # Breakeven sets stop to entry (100); the trail on the same bar lifts it to 102.5 - 2 = 100.5.
    assert pos.stop == pytest.approx(100.5)


def test_long_after_breakeven_stop_exits_at_stop():
    pos = _long()
    evaluate_bar(pos, bar_open=100.0, bar_high=102.5, bar_low=100.0, cfg=CFG)
    price, reason = evaluate_bar(pos, bar_open=100.8, bar_high=100.9, bar_low=99.5, cfg=CFG)
    assert (price, reason) == (pytest.approx(100.5), "stop")


def test_long_trail_follows_best_high_after_breakeven():
    pos = _long()
    pos.target = 200.0  # far target, so the trail is what is tested
    evaluate_bar(pos, bar_open=100.0, bar_high=102.5, bar_low=100.0, cfg=CFG)  # BE
    price, _ = evaluate_bar(pos, bar_open=104.0, bar_high=110.0, bar_low=105.0, cfg=CFG)
    assert price is None
    assert pos.best == 110.0
    assert pos.stop == pytest.approx(108.0)  # 110 - 2 * ATR 1


def test_long_stop_never_loosens():
    pos = _long()
    pos.target = 200.0
    evaluate_bar(pos, bar_open=100.0, bar_high=102.5, bar_low=100.0, cfg=CFG)
    evaluate_bar(pos, bar_open=104.0, bar_high=110.0, bar_low=105.0, cfg=CFG)  # stop 108
    evaluate_bar(pos, bar_open=107.0, bar_high=107.5, bar_low=106.0, cfg=CFG)  # lower high
    assert pos.stop == pytest.approx(108.0)


# --- SELL (mirror) ---

def test_short_stop_hit_exits_at_stop():
    pos = _short()
    price, reason = evaluate_bar(pos, bar_open=101.0, bar_high=102.5, bar_low=100.5, cfg=CFG)
    assert (price, reason) == (102.0, "stop")


def test_short_gap_above_stop_exits_at_open():
    pos = _short()
    price, reason = evaluate_bar(pos, bar_open=104.0, bar_high=104.5, bar_low=103.0, cfg=CFG)
    assert (price, reason) == (104.0, "stop")


def test_short_target_hit_exits_at_target():
    pos = _short()
    price, reason = evaluate_bar(pos, bar_open=99.0, bar_high=99.5, bar_low=93.5, cfg=CFG)
    assert (price, reason) == (94.0, "target")


def test_short_same_bar_stop_wins():
    pos = _short()
    price, reason = evaluate_bar(pos, bar_open=100.0, bar_high=103.0, bar_low=93.0, cfg=CFG)
    assert (price, reason) == (102.0, "stop")


def test_short_trail_follows_best_low():
    pos = _short()
    pos.target = 50.0  # far target, so the trail is what is tested
    evaluate_bar(pos, bar_open=100.0, bar_high=100.0, bar_low=97.5, cfg=CFG)
    # Breakeven at 98 sets stop 100; the trail on the same bar gives 97.5 + 2 = 99.5.
    assert pos.be_done is True and pos.stop == pytest.approx(99.5)
    evaluate_bar(pos, bar_open=96.0, bar_high=96.5, bar_low=90.0, cfg=CFG)
    assert pos.best == 90.0
    assert pos.stop == pytest.approx(92.0)  # 90 + 2 * ATR 1


# --- validation ---

def test_bad_side_rejected():
    with pytest.raises(ValueError):
        Position(key="k", symbol="X", side="HOLD", qty=1, entry=1.0, stop=0.5, target=2.0, atr=1.0)


def test_zero_qty_rejected():
    with pytest.raises(ValueError):
        Position(key="k", symbol="X", side="BUY", qty=0, entry=1.0, stop=0.5, target=2.0, atr=1.0)


def test_stop_equal_entry_rejected():
    with pytest.raises(ValueError):
        Position(key="k", symbol="X", side="BUY", qty=1, entry=1.0, stop=1.0, target=2.0, atr=1.0)


def test_bad_exit_config_rejected():
    with pytest.raises(ValueError):
        ExitConfig(breakeven_r=0.0)
    with pytest.raises(ValueError):
        ExitConfig(trail_mult=-1.0)
