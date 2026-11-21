"""High-volume peg (2026-11-21): ranked bid levels, +2 tick placement, and the three verify checks."""
from types import SimpleNamespace

import pytest

from quant.execution.peg import peg_candidates, peg_level
from quant.execution.peg_slicer import PegSlicer
from quant.execution.peg_verify import BidVerifier, VerifyConfig
from quant.execution.strategy_bot import BotConfig, StrategyBot


def _depth(bids, asks=((100.05, 10),)):
    return {"status": "success", "data": {"bids": [{"price": p, "quantity": q} for p, q in bids],
                                          "asks": [{"price": p, "quantity": q} for p, q in asks]}}


def _quote(ltp, vol):
    return {"status": "success", "data": {"ltp": ltp, "volume": vol}}


# ---- candidates: ranking, depth count, +2 tick, never at the ask -------------------------------
def test_candidates_ranked_by_quantity_largest_first():
    depth = _depth([(100.0, 500), (99.95, 4000), (99.9, 1500)])
    cands = peg_candidates("BUY", depth, tick=0.05, depth_levels=20, offset_ticks=2)
    assert [c[2] for c in cands] == [99.95, 99.9, 100.0]   # level prices, by qty desc


def test_order_price_is_level_plus_two_ticks():
    cands = peg_candidates("BUY", _depth([(99.95, 4000)], asks=[(100.50, 10)]),
                           tick=0.05, depth_levels=20, offset_ticks=2)
    assert cands[0][0] == 100.05 and cands[0][1] == 4000


def test_depth_levels_limits_the_scan():
    bids = [(100.0 - i * 0.05, 100 + i) for i in range(20)]
    deep = peg_candidates("BUY", _depth(bids), tick=0.05, depth_levels=20, offset_ticks=2)
    shallow = peg_candidates("BUY", _depth(bids), tick=0.05, depth_levels=5, offset_ticks=2)
    assert len(deep) == 20 and len(shallow) == 5


def test_offset_never_reaches_the_ask():
    cands = peg_candidates("BUY", _depth([(100.05, 500)], asks=[(100.10, 10)]),
                           tick=0.05, depth_levels=20, offset_ticks=2)
    assert cands[0][0] < 100.10          # would be 100.15; kept at ask minus one tick


def test_sell_is_unchanged_first_ask():
    assert peg_level("SELL", _depth([(100.0, 1)], asks=[(100.05, 7)])) == (100.05, 7)


# ---- verifier: pass path ----------------------------------------------------------------------
def _run(v, sym, rows, level=100.0):
    """rows: (ts, qty_at_level, ltp, cumulative_volume). Samples go in as polls."""
    for ts, qty, ltp, vol in rows:
        bids = [(level, qty)] if qty > 0 else []
        v.observe(sym, ts, _depth(bids + [(99.0, 1000)]), _quote(ltp, vol))
    return v


GOOD = [  # bid 1000 -> sellers take 600 into it (absorbed), it refills, price bounces and holds
    (0, 1000, 100.5, 1000),
    (10, 400, 100.0, 1600),      # 600 traded at the level, qty dropped 600: no pull
    (20, 1000, 100.4, 1600),     # refill, recovery above the level
    (30, 1000, 100.6, 1600),
    (40, 1000, 100.7, 1600),
]


def test_good_level_passes_all_three_checks():
    v = _run(BidVerifier(VerifyConfig(dwell_seconds=10)), "A", GOOD)
    verdict = v.verdict("A", 100.0, now_ts=40)
    assert verdict.ok, verdict.reasons
    assert verdict.metrics["absorb_ratio"] >= 0.5 and verdict.metrics["pull_ratio"] <= 0.3


# ---- verifier: block paths ----------------------------------------------------------------------
def test_pulled_bid_fails_pull_ratio():
    rows = [(0, 1000, 100.5, 1000), (10, 300, 100.5, 1000), (20, 300, 100.6, 1000), (30, 300, 100.7, 1000)]
    v = _run(BidVerifier(), "A", rows)
    reasons = v.verdict("A", 100.0, 30).reasons
    assert "pull_high" in reasons and "absorption_low" in reasons and not v.verdict("A", 100.0, 30).ok


def test_weak_absorption_fails():
    rows = [(0, 1000, 100.5, 1000), (10, 900, 100.0, 1100), (20, 900, 100.4, 1100), (30, 900, 100.6, 1100)]
    assert "absorption_low" in _run(BidVerifier(), "A", rows).verdict("A", 100.0, 30).reasons


def test_no_recovery_after_touch_fails_bounce():
    rows = [(0, 1000, 100.5, 1000), (10, 400, 100.0, 1600), (20, 400, 100.0, 1600), (30, 400, 100.0, 1600)]
    assert "no_bounce" in _run(BidVerifier(), "A", rows).verdict("A", 100.0, 30).reasons


def test_short_dwell_after_bounce_fails():
    rows = [(0, 1000, 100.5, 1000), (10, 400, 100.0, 1600), (20, 1000, 100.4, 1600), (25, 1000, 100.6, 1600)]
    reasons = _run(BidVerifier(), "A", rows).verdict("A", 100.0, 25).reasons
    assert "dwell_short" in reasons   # recovered at 20, held only 5 s (needs 10)


def test_never_touched_fails():
    rows = [(0, 1000, 100.5, 1000), (10, 1000, 100.6, 1000), (20, 1000, 100.7, 1000)]
    assert "no_touch" in _run(BidVerifier(), "A", rows).verdict("A", 100.0, 20).reasons


def test_level_gone_fails():
    rows = [(0, 1000, 100.5, 1000), (10, 0, 100.5, 1000), (20, 0, 100.6, 1000)]
    assert "level_gone" in _run(BidVerifier(), "A", rows).verdict("A", 100.0, 20).reasons


def test_too_few_samples_fails():
    v = _run(BidVerifier(), "A", GOOD[:2])
    assert v.verdict("A", 100.0, 10).reasons == ("insufficient_samples",)


def test_stale_samples_fail():
    v = _run(BidVerifier(), "A", GOOD)
    verdict = v.verdict("A", 100.0, now_ts=500)
    assert not verdict.ok                       # fail-closed; old samples are pruned, so no verdict passes


def test_unusable_depth_is_not_stored():
    v = BidVerifier()
    assert v.observe("A", 0, {"status": "error"}, _quote(100.0, 1)) is False
    assert v.observe("A", 0, _depth([]), _quote(100.0, 1)) is False


# ---- bot: try levels in order, wait when none verifies ------------------------------------------
class _Verifier:
    def __init__(self, ok_levels):
        self.ok_levels, self.asked = set(ok_levels), []

    def verdict(self, sym, level, now_ts):
        self.asked.append(level)
        ok = level in self.ok_levels
        return SimpleNamespace(ok=ok, reasons=() if ok else ("pull_high",), metrics={})


def _bot_stub(verifier, depth, max_levels=3):
    cfg = BotConfig(pov_mode="peg_band_buyer", peg_tick_size=0.05, peg_max_levels=max_levels)
    noop = lambda *a, **k: None  # noqa: E731
    return PegSlicer(cfg=cfg, executor=None, verifier=verifier, depth_provider=lambda s: depth,
                     make_child=None, log=noop, alert=noop, close=noop, book=noop, add_fault=noop)


DEPTH = _depth([(100.0, 5000), (99.0, 3000), (98.0, 1000)])


def test_first_verified_level_is_used_with_two_tick_offset():
    v = _Verifier({99.0})
    bot = _bot_stub(v, DEPTH)
    assert bot.price_now("A", cap=101.0) == (99.10, 3000)
    assert v.asked == [100.0, 99.0]        # highest quantity tried first, then the next
    assert bot.status == "ok"


def test_no_verified_level_means_wait():
    bot = _bot_stub(_Verifier(set()), DEPTH)
    assert bot.price_now("A", cap=101.0) is None
    assert bot.status == "unverified"


def test_only_max_levels_are_tried():
    v = _Verifier({98.0})
    bot = _bot_stub(v, DEPTH, max_levels=2)
    assert bot.price_now("A", cap=101.0) is None
    assert v.asked == [100.0, 99.0]


def test_no_depth_falls_back_not_wait():
    bot = _bot_stub(_Verifier({100.0}), None)
    bot._depth = None
    assert bot.price_now("A", cap=101.0) is None
    assert bot.status == "nodepth"


def test_missing_verifier_waits_fail_closed():
    bot = _bot_stub(None, DEPTH)
    assert bot.price_now("A", cap=101.0) is None
    assert bot.status == "unverified"


# ---- config validation -------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs", [
    {"peg_depth_levels": 0}, {"peg_depth_levels": 51}, {"peg_offset_ticks": 11},
    {"peg_absorb_ratio": 0}, {"peg_max_pull_ratio": 1.0}, {"peg_dwell_seconds": 0},
    {"peg_verify_window_seconds": 5}, {"peg_max_levels": 0},
])
def test_bad_peg_config_is_refused(kwargs):
    with pytest.raises(ValueError):
        BotConfig(**kwargs)


def test_peg_defaults_match_the_spec():
    cfg = BotConfig()
    assert (cfg.peg_depth_levels, cfg.peg_offset_ticks, cfg.peg_max_levels) == (20, 2, 3)
