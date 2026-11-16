"""Buyer-initiated VWAP from quote polls (2026-11-16). Rule: volume added between polls counts
as buyer-initiated only when LTP >= ask at the poll."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from quant.execution.buyer_vwap import BuyerVwapBook
from quant.execution.strategy_bot import BotConfig, StrategyBot, read_screen  # noqa: F401

NOW = datetime(2026, 11, 16, 10, 0)
DAY = NOW.date().isoformat()


def _q(ltp, ask, volume):
    return {"ltp": ltp, "ask": ask, "volume": volume}


def test_first_poll_is_baseline_only():
    book = BuyerVwapBook(None, "k")
    assert book.apply_quote("A", _q(100, 99, 1000), DAY) is False
    assert book.vwap("A") is None


def test_buyer_volume_only_when_ltp_at_or_above_ask():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 1000), DAY)            # baseline
    assert book.apply_quote("A", _q(101, 100, 1200), DAY)     # buyer: 200 @ 101
    assert book.apply_quote("A", _q(99, 100, 1500), DAY) is False  # seller: 300 not counted
    assert book.vwap("A") == pytest.approx(101.0)
    snap = book.snapshot()["A"]
    assert snap["buyer_volume"] == 200 and snap["counted_polls"] == 1


def test_vwap_is_volume_weighted_across_polls():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 0), DAY)
    book.apply_quote("A", _q(100, 100, 100), DAY)   # 100 @ 100
    book.apply_quote("A", _q(110, 109, 400), DAY)   # 300 @ 110
    assert book.vwap("A") == pytest.approx((100 * 100 + 110 * 300) / 400)


def test_volume_reset_or_no_growth_adds_nothing():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 500), DAY)
    assert book.apply_quote("A", _q(100, 99, 500), DAY) is False   # no growth
    assert book.apply_quote("A", _q(100, 99, 100), DAY) is False   # reset (volume went down)
    assert book.vwap("A") is None


def test_missing_ask_or_ltp_is_not_counted_but_baseline_moves():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 100), DAY)
    assert book.apply_quote("A", {"ltp": 100, "volume": 300}, DAY) is False
    assert book.apply_quote("A", _q(None, 99, 400), DAY) is False
    assert book.vwap("A") is None
    assert book.apply_quote("A", _q(100, 99, 500), DAY)            # next interval counts from 400


def test_new_day_starts_again():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 0), DAY)
    book.apply_quote("A", _q(100, 100, 100), DAY)
    assert book.vwap("A") == pytest.approx(100.0)
    tomorrow = "2026-11-17"
    book.apply_quote("A", _q(120, 119, 50), tomorrow)              # baseline for the new day
    assert book.vwap("A") is None


class _Reader:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def quotes(self, key, symbol, exchange):
        self.calls.append((symbol, exchange))
        r = self.replies[symbol]
        if isinstance(r, Exception):
            raise r
        return r


def test_poll_reports_each_symbol_and_survives_errors():
    reader = _Reader({
        "A": {"status": "success", "data": _q(100, 99, 0)},
        "B": RuntimeError("timeout"),
        "C": {"status": "error", "message": "bad symbol"},
    })
    book = BuyerVwapBook(reader, "k", exchange="NSE")
    out = book.poll(["A", "B", "C"], NOW)
    assert out == {"A": "not counted", "B": "error: RuntimeError('timeout')", "C": "no quote"}
    assert reader.calls == [("A", "NSE"), ("B", "NSE"), ("C", "NSE")]


def test_bot_band_mode_uses_book_vwap_when_available():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 0), DAY)
    book.apply_quote("A", _q(100, 100, 100), DAY)
    cfg = BotConfig(pov_mode="band", pov=0.1)
    import pandas as pd
    df = pd.DataFrame({"close": [100.0, 100.0, 100.0]})
    fake = SimpleNamespace(cfg=cfg, _vwap_provider=book.vwap, _log=lambda *a, **k: None)
    rate, reason = StrategyBot._pov_for(fake, "A", df, "BUY")
    assert reason != "fallback"           # the verified VWAP is used, not the fallback


def test_bot_refuses_bad_poll_interval():
    with pytest.raises(ValueError):
        BotConfig(vwap_poll_seconds=1)


def test_buyer_volume_and_share_count_only_classifiable_volume():
    book = BuyerVwapBook(None, "k")
    book.apply_quote("A", _q(100, 99, 0), DAY)
    book.apply_quote("A", _q(100, 100, 100), DAY)     # buyer 100
    book.apply_quote("A", _q(99, 100, 200), DAY)      # seller 100
    book.apply_quote("A", {"ltp": 100, "volume": 300}, DAY)  # no ask: unclassified, not in share
    assert book.buyer_volume("A") == 100
    assert book.buyer_share("A") == pytest.approx(0.5)
    snap = book.snapshot()["A"]
    assert snap["classified_volume"] == 200 and snap["buyer_share"] == pytest.approx(0.5)


def test_buyer_volume_none_before_any_poll_today():
    book = BuyerVwapBook(None, "k")
    assert book.buyer_volume("ZZZ") is None and book.buyer_share("ZZZ") is None


def test_buyer_mode_scales_volume_by_buyer_share():
    cfg = BotConfig(pov_mode="buyer", pov=0.1)
    fake = SimpleNamespace(cfg=cfg, _buyer_share_provider=lambda sym: 0.5, _log=lambda *a, **k: None)
    assert StrategyBot._slice_volume(fake, "A", 1000.0) == (500.0, "buyer")


def test_buyer_mode_falls_back_to_full_volume_without_share():
    cfg = BotConfig(pov_mode="buyer", pov=0.1)
    fake = SimpleNamespace(cfg=cfg, _buyer_share_provider=lambda sym: None, _log=lambda *a, **k: None)
    assert StrategyBot._slice_volume(fake, "A", 1000.0) == (1000.0, "fallback")


def test_fixed_mode_ignores_buyer_share():
    cfg = BotConfig(pov_mode="fixed", pov=0.1)
    fake = SimpleNamespace(cfg=cfg, _buyer_share_provider=lambda sym: 0.1, _log=lambda *a, **k: None)
    assert StrategyBot._slice_volume(fake, "A", 1000.0) == (1000.0, "volume")


def test_child_qty_uses_buyer_volume_only():
    from quant.execution.router import pov_child_qty
    # 10% of 500 buyer shares = 50 (not 100 from the full 1000)
    assert pov_child_qty(1000, 1000.0 * 0.5, 0.1) == 50
