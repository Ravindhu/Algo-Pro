"""PCAS pre-trade check: timing, limit-only, self-cross (decision 0025).

Flow stage: Risk gate
Status: TEST
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from quant.risk.pcas_check import pcas_order_check

IST = ZoneInfo("Asia/Kolkata")


def at(hhmm):
    h, m = hhmm.split(":")
    return datetime(2026, 11, 6, int(h), int(m), tzinfo=IST)


def test_limit_order_in_safe_window_allowed():
    assert pcas_order_check(at("10:00"), "BUY", "LIMIT", 100.0) == (True, ())


def test_market_order_refused_even_in_safe_window():
    ok, reasons = pcas_order_check(at("10:00"), "BUY", "MARKET", None)
    assert ok is False
    assert any("limit-only" in r for r in reasons)


def test_market_order_refused_for_any_price_type_other_than_limit():
    for kind in ("MARKET", "SL", "PEG", ""):
        assert pcas_order_check(at("10:00"), "SELL", kind, None)[0] is False


def test_limit_order_at_random_close_minute_refused():
    ok, reasons = pcas_order_check(at("10:14"), "BUY", "LIMIT", 100.0)
    assert ok is False and any("PCAS timing" in r for r in reasons)


def test_limit_order_outside_sessions_refused():
    assert pcas_order_check(at("09:00"), "BUY", "LIMIT", 100.0)[0] is False


def test_self_cross_refused_inside_safe_window():
    ok, reasons = pcas_order_check(at("10:00"), "BUY", "LIMIT", 100.0, resting=[("SELL", 100.0)])
    assert ok is False and any("self-cross" in r for r in reasons)


def test_all_failures_are_reported_together():
    ok, reasons = pcas_order_check(at("10:14"), "BUY", "MARKET", None, resting=[("SELL", 100.0)])
    assert ok is False
    assert len(reasons) == 2  # timing and limit-only; self-cross is skipped for non-limit
