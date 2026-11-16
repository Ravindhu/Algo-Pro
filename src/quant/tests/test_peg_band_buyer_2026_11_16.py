"""Bounded peg (2026-11-16): entries as LIMIT at the best bid, reprice up to peg_max_attempts,
fall back to fixed volume when any input is missing."""
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.execution import strategy_bot as sb
from quant.execution.peg import peg_level, peg_price
from quant.execution.strategy_bot import AnalyzeSender, BotConfig, StrategyBot


def _depth(bid, ask=None):
    return {"status": "success", "data": {"bids": [{"price": bid, "quantity": 10}],
                                          "asks": [{"price": ask or bid + 0.05, "quantity": 10}]}}


# ---- pricing ----------------------------------------------------------------
def test_peg_is_best_bid_for_buy_and_best_ask_for_sell():
    assert peg_price("BUY", _depth(100.0, 100.05)) == 100.0
    assert peg_price("SELL", _depth(100.0, 100.05)) == 100.05


def test_peg_refuses_missing_bad_or_over_cap_prices():
    assert peg_price("BUY", None) is None
    assert peg_price("BUY", {"status": "error"}) is None
    assert peg_price("BUY", {"status": "success", "data": {"bids": []}}) is None
    assert peg_price("BUY", _depth(float("nan"))) is None
    assert peg_price("BUY", _depth(100.0), cap=99.0) is None   # over the cap: fixed path


# ---- bounded reprice -----------------------------------------------------------
class _Outcome(SimpleNamespace):
    pass


def _lvl(px, qty=1000):
    return None if px is None else (px, qty)


def _fake_bot(replies, bids, max_attempts=3):
    """replies: list of response dicts returned by submit_child, in order. bids: prices from depth."""
    cfg = BotConfig(pov_mode="peg_band_buyer", peg_max_attempts=max_attempts)
    calls = []

    class _Exec:
        def submit_child(self, child, price_type, limit_px):
            calls.append((child, price_type, limit_px))
            return _Outcome(sent=True, response=replies[len(calls) - 1])

    bid_iter = iter(bids)
    fake = SimpleNamespace(cfg=cfg, _executor=_Exec(), _log=lambda *a, **k: None,
                           _peg_price_now=lambda sym, cap: _lvl(next(bid_iter, None)))
    p = SimpleNamespace(intent=SimpleNamespace(symbol="A"), key="K", seq=0)
    return fake, p, calls


@pytest.fixture(autouse=True)
def _fake_child(monkeypatch):
    monkeypatch.setattr(sb.PovExecutor, "make_child", staticmethod(lambda intent, qty, seq: ("child", qty, seq)))


def test_fill_on_first_attempt_is_one_submit():
    fake, p, calls = _fake_bot([{"status": "success", "avg_price": 100.0}], bids=[])
    out, child = StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
    assert len(calls) == 1 and calls[0][1:] == ("LIMIT", 100.0)
    assert out.response["status"] == "success"


def test_clean_cancel_reprices_from_current_bid():
    unfilled = {"status": "error", "reason": "limit not filled (open); cancelled"}
    fake, p, calls = _fake_bot([unfilled, {"status": "success", "avg_price": 100.2}],
                               bids=[100.2])
    out, _ = StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
    assert [c[2] for c in calls] == [100.0, 100.2]
    assert out.response["status"] == "success"


def test_reprices_stop_at_max_attempts():
    unfilled = {"status": "error", "reason": "limit not filled (open); cancelled"}
    fake, p, calls = _fake_bot([unfilled] * 5, bids=[100.1, 100.2, 100.3], max_attempts=2)
    StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
    assert len(calls) == 2


def test_partial_or_failed_cancel_never_reprices():
    for resp in ({"status": "error", "partial": True, "reason": "limit not filled (partial)"},
                 {"status": "error", "cancel_failed": True, "reason": "limit not filled (open); cancel failed"}):
        fake, p, calls = _fake_bot([resp], bids=[100.1])
        StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
        assert len(calls) == 1


def test_bid_above_cap_stops_reprice():
    unfilled = {"status": "error", "reason": "limit not filled (open); cancelled"}
    fake, p, calls = _fake_bot([unfilled, unfilled], bids=[None])
    StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
    assert len(calls) == 1


# ---- sender waits before checking a LIMIT ---------------------------------------
class _Client:
    def __init__(self):
        self.status_calls = 0

    def place_child(self, intent, api_key, decision, price_type, limit_price):
        return {"status": "success", "orderid": "1"}

    def get_order_status(self, api_key, order_id, strategy):
        self.status_calls += 1
        return {"data": {"order_status": "complete", "average_price": 100.0}}


def test_sender_waits_only_for_limit_when_configured():
    slept = []
    s = AnalyzeSender(_Client())
    s._sleep = slept.append
    s.limit_wait_seconds = 5.0
    intent = SimpleNamespace(strategy_id="s", qty=1)
    s.place_child(intent, "k", None, "LIMIT", 100.0)
    s.place_child(intent, "k", None, "MARKET", None)
    assert slept == [5.0]


def test_peg_mode_refused_setting_is_validated():
    with pytest.raises(ValueError):
        BotConfig(pov_mode="peg_band_buyer", peg_max_attempts=9)
    with pytest.raises(ValueError):
        BotConfig(pov_mode="peg_band_buyer", peg_wait_seconds=0)


def test_peg_level_returns_price_and_size_at_best_bid():
    assert peg_level("BUY", _depth(100.0)) == (100.0, 10)
    assert peg_level("BUY", _depth(100.0), cap=99.0) is None


def test_reprice_caps_child_to_size_at_new_best_bid():
    unfilled = {"status": "error", "reason": "limit not filled (open); cancelled"}
    fake, p, calls = _fake_bot([unfilled, {"status": "success", "avg_price": 100.2}], bids=[100.2])
    # the reprice sees only 4 shares at the bid; with fraction 0.5 the child may take 2
    fake._peg_price_now = lambda sym, cap: (100.2, 4)
    StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
    assert calls[0][0][1] == 10 and calls[1][0][1] == 2


def test_thin_reprice_bid_stops_the_chain():
    unfilled = {"status": "error", "reason": "limit not filled (open); cancelled"}
    fake, p, calls = _fake_bot([unfilled, unfilled], bids=[100.2])
    fake._peg_price_now = lambda sym, cap: (100.2, 1)   # 1 share at bid x 0.5 -> 0 shares: stop
    StrategyBot._submit_peg(fake, p, 10, 100.0, 101.0)
    assert len(calls) == 1


def test_peg_bid_fraction_is_validated():
    with pytest.raises(ValueError):
        BotConfig(pov_mode="peg_band_buyer", peg_bid_fraction=0)


def test_peg_one_tick_above_best_bid_when_below_ask():
    assert peg_level("BUY", _depth(100.0, 100.20), None, 0.05) == (100.05, 10)


def test_peg_never_reaches_the_ask():
    # bid + tick would equal the ask: stay at the bid, do not cross the spread
    assert peg_level("BUY", _depth(100.0, 100.05), None, 0.05) == (100.0, 10)


def test_peg_tick_off_keeps_best_bid_and_cap_applies_after_tick():
    assert peg_level("BUY", _depth(100.0, 100.20), None, 0.0) == (100.0, 10)
    assert peg_level("BUY", _depth(100.0, 100.20), 100.04, 0.05) is None


def test_peg_tick_size_is_validated():
    with pytest.raises(ValueError):
        BotConfig(pov_mode="peg_band_buyer", peg_tick_size=-0.1)
