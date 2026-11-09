"""Parity (decouple 6.5, Phase C): local PEG engine and liquidity core vs the real Blue modules.

Runs in the sandbox through conftest.blue. Each case feeds the same snapshot sequence to
Blue and to the local port and compares every decision.
"""
import numpy as np
import pytest

from quant.execution import pov_liquidity as local_liq
from quant.execution import pov_guards as local_guards
from quant.execution import pov_peg as local_peg


def _tob_kwargs(rng, has_top=True):
    bid = float(rng.uniform(90, 110))
    ask = bid + float(rng.choice([0.05, 0.10, 0.20]))
    return dict(best_bid=bid, bid_qty=100, best_ask=ask, ask_qty=100,
                ltp=float(rng.uniform(bid, ask)), upper_circuit=ask * 1.2,
                lower_circuit=bid * 0.8, tick_size=0.05, auction_price=None,
                auction_volume=None, source="depth")


@pytest.mark.parametrize("seed", range(40))
def test_peg_sequence_matches_blue(blue, seed):
    peg_b = blue("pov.slicers.peg")
    rng = np.random.default_rng(seed)
    action = "BUY" if seed % 2 == 0 else "SELL"
    cfg = {"peg_max_chase_ticks": int(rng.integers(0, 4)),
           "peg_patience_seconds": float(rng.uniform(2, 15)),
           "peg_escape_buffer_ticks": int(rng.integers(0, 3))}
    eng_b, eng_l = peg_b.PegEngine(cfg), local_peg.PegEngine(cfg)
    kw = _tob_kwargs(rng)
    now = 1000.0
    for step in range(12):
        # Random walk of the touch so repegs, runaway and patience paths all get exercised.
        drift = float(rng.choice([-0.10, 0.0, 0.05, 0.15]))
        kw["best_bid"] = round(kw["best_bid"] + drift, 2)
        kw["best_ask"] = round(kw["best_bid"] + 0.05 * int(rng.integers(1, 4)), 2)
        now += float(rng.uniform(0.5, 4.0))
        want = eng_b.decide(peg_b.TopOfBook(**kw), action=action, now_ts=now)
        got = eng_l.decide(local_guards.TopOfBook(**kw), action=action, now_ts=now)
        assert got.reason == want.reason, f"step {step}"
        if want.price is None:
            assert got.price is None
        else:
            assert got.price == pytest.approx(want.price, abs=1e-9), f"step {step}"


def test_peg_reset_matches_blue(blue):
    peg_b = blue("pov.slicers.peg")
    cfg = {}
    rng = np.random.default_rng(7)
    kw = _tob_kwargs(rng)
    eng_b, eng_l = peg_b.PegEngine(cfg), local_peg.PegEngine(cfg)
    for eng_top in ((eng_b, peg_b.TopOfBook), (eng_l, local_guards.TopOfBook)):
        eng, TopOfBook = eng_top
        eng.decide(TopOfBook(**kw), action="BUY", now_ts=0.0)
        eng.reset_for_new_slice()
    want = eng_b.decide(peg_b.TopOfBook(**kw), action="BUY", now_ts=5.0)
    got = eng_l.decide(local_guards.TopOfBook(**kw), action="BUY", now_ts=5.0)
    assert (got.reason, got.price) == (want.reason, pytest.approx(want.price))


def test_peg_config_validation_matches_blue(blue):
    peg_b = blue("pov.slicers.peg")
    for cfg in ({}, {"peg_max_chase_ticks": -1}, {"peg_patience_seconds": 0},
                {"peg_escape_buffer_ticks": -2}, {"peg_patience_seconds": "abc"}):
        assert (local_peg.validate_peg_config(cfg) is None) == (peg_b.validate_peg_config(cfg) is None)


@pytest.mark.parametrize("value", [0.0, -5.0, 1.0, 9_999_999.0, 10_000_000.0, 99_999_999.0,
                                   100_000_000.0, 499_999_999.0, 500_000_000.0, 999_999_999.0,
                                   1_000_000_000.0, 5e9, float("nan"), None, "x"])
def test_tier_resolution_matches_blue(blue, value):
    liq_b = blue("core.liquidity")
    assert local_liq.resolve_liquidity_tier(value) == liq_b.resolve_liquidity_tier(value)


@pytest.mark.parametrize("seed", range(40))
def test_profile_and_child_qty_match_blue(blue, seed):
    liq_b = blue("core.liquidity")
    rng = np.random.default_rng(seed)
    symbol_vol = int(rng.integers(0, 50_000_000))
    px = float(rng.uniform(1, 5000))
    target_inv = float(rng.uniform(0, 1e8))
    pb = liq_b.build_liquidity_profile("X", "NSE", symbol_vol, px, target_inv)
    pl = local_liq.build_liquidity_profile("X", "NSE", symbol_vol, px, target_inv)
    assert pl.tier_name == pb.tier_name
    assert pl.daily_liquidity_value == pytest.approx(pb.daily_liquidity_value, rel=1e-12)
    assert pl.investment_weight == pytest.approx(pb.investment_weight, rel=1e-12)
    assert pl.max_block_size_pct == pytest.approx(pb.max_block_size_pct, rel=1e-12)
    target = int(rng.integers(1, 5000))
    remaining = int(rng.integers(0, target + 1))
    part = float(rng.uniform(0, 30))
    assert local_liq.calculate_max_child_qty(pl, remaining, target, part) == \
        liq_b.calculate_max_child_qty(pb, remaining, target, part)
    assert local_liq.scale_block_size_pct(pl, remaining, target, part) == \
        pytest.approx(liq_b.scale_block_size_pct(pb, remaining, target, part), rel=1e-12)


@pytest.mark.parametrize("tiers", [None, {"MID": 0.02, "MICRO": 0.5}])
def test_custom_tiers_match_blue(blue, tiers):
    liq_b = blue("core.liquidity")
    for vol in (1_000, 2_000_000, 50_000_000):
        pb = liq_b.build_liquidity_profile("X", "BSE", vol, 150.0, 1e6, custom_tiers=tiers)
        pl = local_liq.build_liquidity_profile("X", "BSE", vol, 150.0, 1e6, custom_tiers=tiers)
        assert (pl.tier_name, pl.max_block_size_pct) == (pb.tier_name, pytest.approx(pb.max_block_size_pct))
