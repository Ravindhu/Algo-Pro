"""Hand-checked tests for POV Phase B (band, guards, sizing). Numbers worked out by hand."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.execution.pov_band import BandConfig, compute_target_pov_rate
from quant.execution.pov_guards import TopOfBook, evaluate_guards
from quant.execution.pov_sizing import clamp_child_qty, poll_slice_qty, slippage_penalised_rate

IST = ZoneInfo("Asia/Kolkata")


def test_band_midpoint_hand_checked():
    # vwap 1000: ceiling 1005, floor 970. ltp 987.5 -> f = (1005-987.5)/(1005-970) = 0.5
    # rate = lerp(5, 25, 0.5) = 15
    d = compute_target_pov_rate(ltp=987.5, config=BandConfig(vwap=1000.0))
    assert d.rate == pytest.approx(15.0)
    assert d.ceiling == pytest.approx(1005.0) and d.floor == pytest.approx(970.0)


def test_depth_pricing_buy_and_sell_hand_checked():
    top = TopOfBook(best_bid=100.00, bid_qty=50, best_ask=100.10, ask_qty=50, ltp=100.05, tick_size=0.05)
    buy = evaluate_guards(top, action="BUY", cfg={"slice_qty": 5, "depth_offset_ticks": 1})
    sell = evaluate_guards(top, action="SELL", cfg={"slice_qty": 5, "depth_offset_ticks": 1})
    assert buy.new_price == pytest.approx(100.05)   # bid + 1 tick, below the ask
    assert sell.new_price == pytest.approx(100.05)  # ask - 1 tick, above the bid


def test_slice_cap_to_opposite_depth_hand_checked():
    top = TopOfBook(best_bid=100.0, bid_qty=60, best_ask=100.1, ask_qty=40, ltp=100.05, tick_size=0.05)
    d = evaluate_guards(top, action="BUY", cfg={"slice_qty": 100})
    assert d.capped_qty == 40                       # min(100, ask_qty 40)
    empty = TopOfBook(best_bid=100.0, best_ask=100.1, ltp=100.05, tick_size=0.05)
    assert evaluate_guards(empty, action="BUY", cfg={"slice_qty": 100}).capped_qty == 100  # no depth: legacy


def test_slice_below_min_qty_is_skipped():
    top = TopOfBook(best_bid=100.0, bid_qty=60, best_ask=100.1, ask_qty=2, ltp=100.05, tick_size=0.05)
    d = evaluate_guards(top, action="BUY", cfg={"slice_qty": 100, "depth_slice_min_qty": 5})
    assert d.capped_qty == 0 and d.metadata["skip"] == "below_min_qty"


def test_circuit_pause_hand_checked():
    # upper 100, buffer 0.5% -> threshold 99.5
    near = TopOfBook(best_bid=99.5, best_ask=99.6, ltp=99.6, upper_circuit=100.0, tick_size=0.05)
    far = TopOfBook(best_bid=98.0, best_ask=98.1, ltp=99.4, upper_circuit=100.0, tick_size=0.05)
    assert evaluate_guards(near, action="BUY", cfg={}).pause_reason == "NEAR_UPPER_CIRCUIT"
    assert evaluate_guards(far, action="BUY", cfg={}).pause_reason is None


def test_missing_ltp_fails_closed():
    top = TopOfBook(best_bid=100.0, best_ask=100.1, tick_size=0.05)
    assert evaluate_guards(top, action="BUY", cfg={}).pause_reason == "MARKET_DATA_UNAVAILABLE"


def test_auction_window_uses_explicit_clock():
    cfg_in = {"auction_windows": ["09:00-09:15"], "now": datetime(2026, 11, 9, 9, 5, tzinfo=IST)}
    cfg_out = {"auction_windows": ["09:00-09:15"], "now": datetime(2026, 11, 9, 10, 0, tzinfo=IST)}
    top = TopOfBook(best_bid=100.0, best_ask=100.1, ltp=100.05, tick_size=0.05)
    assert evaluate_guards(top, action="BUY", cfg=cfg_in).pause_reason is None
    assert evaluate_guards(top, action="BUY", cfg=cfg_out).pause_reason == "OUTSIDE_AUCTION_WINDOW"


def test_auction_price_guard_alone_anchors_sme():
    # The guard itself returns the auction price for SME/PCAS listings.
    from quant.execution.pov_guards import AuctionPriceGuard
    top = TopOfBook(best_bid=100.0, best_ask=100.1, ltp=100.05, auction_price=50.0, tick_size=0.05)
    d = AuctionPriceGuard().evaluate(top, action="BUY", cfg={"sme_periodic_call_auction": True})
    assert d.new_price == pytest.approx(50.0)


def test_known_issue_depth_price_overrides_auction_price_in_aggregator():
    # FINDING (Blue behaviour, copied on purpose for parity): evaluate_guards is rightmost-wins, so
    # DepthPricingGuard (runs after AuctionPriceGuard) overrides the SME auction anchor.
    # The module comments say the auction price should win. Decision pending (see plan doc).
    top = TopOfBook(best_bid=100.0, best_ask=100.1, ltp=100.05, auction_price=50.0, tick_size=0.05)
    d = evaluate_guards(top, action="BUY", cfg={"sme_periodic_call_auction": True})
    assert d.new_price == pytest.approx(100.05)


def test_child_size_hand_checked():
    assert poll_slice_qty(1000, 15.0) == 150
    assert slippage_penalised_rate(15.0, over_threshold=True, penalty=0.5, floor_pct=5.0) == pytest.approx(7.5)
    assert slippage_penalised_rate(15.0, over_threshold=True, penalty=0.1, floor_pct=5.0) == pytest.approx(5.0)


def test_clamp_order_remaining_then_max_slice_then_lot():
    assert clamp_child_qty(150, remaining=100, max_slice=None, lot_size=None) == 100
    assert clamp_child_qty(150, remaining=500, max_slice=40, lot_size=None) == 40
    assert clamp_child_qty(100, remaining=500, max_slice=None, lot_size=25) == 100
    assert clamp_child_qty(90, remaining=500, max_slice=None, lot_size=25) == 75   # floor to lot
    assert clamp_child_qty(10, remaining=500, max_slice=None, lot_size=25) == 0    # lot consumed
    assert clamp_child_qty(50, remaining=0, max_slice=None, lot_size=None) == 0
