"""Parity (decouple 6.5, Phase B): local POV band, guards and sizing vs the real Blue modules.

Runs in the sandbox through conftest.blue. Auction-window cases use an explicit clock in the
hand-checked tests (src/quant/tests/test_pov_phase_b.py), not here.
"""
import numpy as np
import pytest

from quant.execution import pov_band as local_band
from quant.execution import pov_guards as local_guards
from quant.execution.pov_sizing import clamp_child_qty, poll_slice_qty, slippage_penalised_rate


@pytest.mark.parametrize("seed", range(30))
def test_band_rate_matches_blue(blue, seed):
    band = blue("core.band")
    rng = np.random.default_rng(seed)
    vwap = float(rng.uniform(50, 2000))
    kwargs = dict(vwap_ceiling_pct=float(rng.uniform(0.1, 1.0)), vwap_floor_pct=float(rng.uniform(1, 5)),
                  min_pov_pct=5.0, max_pov_pct=25.0, part_pct=float(rng.uniform(5, 15)),
                  passthrough_floor=["part_pct", "max_pov", "min_pov"][seed % 3],
                  action=["BUY", "SELL"][seed % 2])
    for ltp in [vwap * f for f in (0.90, 0.97, 0.99, 1.0, 1.003, 1.01, 1.1)] + [float("nan"), 0.0]:
        want = band.compute_target_pov_rate(ltp=ltp, config=band.BandConfig(vwap=vwap, **kwargs))
        got = local_band.compute_target_pov_rate(ltp=ltp, config=local_band.BandConfig(vwap=vwap, **kwargs))
        assert got.rate == pytest.approx(want.rate, rel=1e-12, abs=1e-12)
        assert got.reason == want.reason


def _tob(rng):
    bid = float(rng.uniform(90, 110))
    ask = bid + float(rng.choice([0.05, 0.1, 0.2, 0.5]))
    return dict(best_bid=bid, bid_qty=int(rng.integers(0, 500)), best_ask=ask,
                ask_qty=int(rng.integers(0, 500)), ltp=float(rng.uniform(bid, ask)),
                upper_circuit=float(ask * 1.2), lower_circuit=float(bid * 0.8),
                tick_size=0.05, auction_price=None, auction_volume=None, source="depth")


@pytest.mark.parametrize("seed", range(40))
def test_guards_match_blue_without_auction_window(blue, seed):
    bg = blue("pov.slicers.guards")
    rng = np.random.default_rng(seed)
    kw = _tob(rng)
    top_b, top_l = bg.TopOfBook(**kw), local_guards.TopOfBook(**kw)
    cfg = {"slice_qty": int(rng.integers(1, 300)), "depth_offset_ticks": int(rng.integers(0, 3)),
           "circuit_buffer_pct": 0.5, "depth_slice_min_qty": int(rng.integers(1, 20))}
    for action in ("BUY", "SELL"):
        want = bg.evaluate_guards(top_b, action=action, cfg=cfg)
        got = local_guards.evaluate_guards(top_l, action=action, cfg=cfg)
        assert got.pause_reason == want.pause_reason
        assert got.capped_qty == want.capped_qty
        if want.new_price is None:
            assert got.new_price is None
        else:
            assert got.new_price == pytest.approx(want.new_price, abs=1e-9)


@pytest.mark.parametrize("seed", range(15))
def test_normalise_matches_blue(blue, seed):
    bg = blue("pov.slicers.guards")
    rng = np.random.default_rng(seed)
    bid = float(rng.uniform(90, 110))
    depth = {"status": "success", "data": {"bids": [{"price": bid, "quantity": 40}],
                                           "asks": [{"price": bid + 0.1, "quantity": 25}]}}
    quote = {"status": "success", "data": {"ltp": bid + 0.05, "upper_circuit_limit": bid * 1.2,
                                           "lower_circuit_limit": bid * 0.8, "auction_price": bid}}
    want = bg.normalize_top_of_book(depth_resp=depth, quote_resp=quote, cfg={})
    got = local_guards.normalize_top_of_book(depth_resp=depth, quote_resp=quote, cfg={})
    assert got == local_guards.TopOfBook(**{f: getattr(want, f) for f in want.__dataclass_fields__})


def test_sizing_helpers_match_blue_formulas():
    for delta in (0, 1, 37, 1250, 99999):
        for rate in (0.0, 5.0, 12.5, 25.0):
            assert poll_slice_qty(delta, rate) == int((delta * rate) / 100.0)
    for seed in range(10):
        rng = np.random.default_rng(seed)
        rate = float(rng.uniform(0, 30))
        pen = float(rng.uniform(0, 1))
        floor = float(rng.uniform(0, 10))
        assert slippage_penalised_rate(rate, over_threshold=True, penalty=pen, floor_pct=floor) == \
            max(floor, rate * max(0.0, pen))
        assert slippage_penalised_rate(rate, over_threshold=False, penalty=pen, floor_pct=floor) == rate
