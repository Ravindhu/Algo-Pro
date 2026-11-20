"""Tests for the read-only portfolio header (decision 0032). No broker, no network: fake transport only."""
import math

from quant.execution.openalgo_client import OpenAlgoClient
from quant.reporting.portfolio_view import MAX_ROWS, HoldingsReader, build_portfolio

AS_OF = "2026-11-09T12:00:00+05:30"


def _reply(n_rows=2, stats=None):
    holdings = [{"symbol": f"SYM{i}", "exchange": "NSE", "quantity": 10, "average_price": 100.0,
                 "ltp": 110.5, "pnl": 105.0, "pnlpercent": 10.5} for i in range(n_rows)]
    return {"status": "success", "data": {"holdings": holdings,
            "statistics": stats if stats is not None else {"totalholdingvalue": 2210.0,
                                                           "totalinvvalue": 2000.0,
                                                           "totalprofitandloss": 210.0,
                                                           "totalpnlpercentage": 10.5}}}


def test_success_reply_maps_summary_and_rows():
    p = build_portfolio(_reply(2), AS_OF)
    assert p["state"] == "broker_read" and p["error"] is None
    assert p["summary"] == {"value": 2210.0, "invested": 2000.0, "pnl": 210.0, "pnl_pct": 10.5, "count": 2}
    assert [r["symbol"] for r in p["holdings"]] == ["SYM0", "SYM1"]
    assert p["holdings"][0]["ltp"] == 110.5 and p["truncated"] is False


def test_rows_are_capped_and_flagged():
    p = build_portfolio(_reply(MAX_ROWS + 5), AS_OF)
    assert len(p["holdings"]) == MAX_ROWS and p["truncated"] is True
    assert p["summary"]["count"] == MAX_ROWS + 5


def test_missing_or_bad_numbers_stay_none_never_estimated():
    stats = {"totalholdingvalue": "2210", "totalinvvalue": True, "totalprofitandloss": math.nan}
    p = build_portfolio(_reply(1, stats=stats), AS_OF)
    assert p["summary"]["value"] is None and p["summary"]["invested"] is None and p["summary"]["pnl"] is None
    assert p["summary"]["pnl_pct"] is None


def test_non_success_reply_is_error_state():
    p = build_portfolio({"status": "error", "message": "Invalid openalgo apikey"}, AS_OF)
    assert p["state"] == "error" and p["error"] == "Invalid openalgo apikey"
    assert p["summary"] is None and p["holdings"] == []
    assert build_portfolio(None, AS_OF)["state"] == "error"


def test_reader_caches_within_ttl_and_refetches_after():
    calls, now = [], [1000.0]

    def fetch(key):
        calls.append(key)
        return _reply(1)

    reader = HoldingsReader(fetch, lambda: "KEY", clock=lambda: now[0], ttl_s=30)
    assert reader.read()["state"] == "broker_read"
    now[0] += 10
    reader.read()
    assert len(calls) == 1
    now[0] += 25
    reader.read()
    assert len(calls) == 2


def test_missing_key_is_not_connected_and_never_calls_broker():
    calls = []

    def no_key():
        raise RuntimeError("OPENALGO_API_KEY is not set")

    reader = HoldingsReader(lambda k: calls.append(k) or _reply(1), no_key, clock=lambda: 1000.0)
    p = reader.read()
    assert p["state"] == "not_connected" and calls == []


def test_broker_failure_is_error_without_leaking_secret():
    def boom(key):
        raise OSError(f"connection refused for key {key}")

    p = HoldingsReader(boom, lambda: "SECRET-KEY-123", clock=lambda: 1000.0).read()
    assert p["state"] == "error"
    assert "SECRET-KEY-123" not in p["error"] and p["error"] == "OpenAlgo unreachable (OSError)"


def test_get_holdings_posts_read_only_to_holdings_path():
    sent = []

    def transport(url, payload):
        sent.append((url, payload))
        return _reply(0)

    client = OpenAlgoClient("http://127.0.0.1:5000/", transport=transport, mode="analyze")
    client.get_holdings("KEY")
    assert sent == [("http://127.0.0.1:5000/api/v1/holdings", {"apikey": "KEY"})]
    assert "placeorder" not in sent[0][0]


# ---- hold days (tradebook) ----
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from quant.reporting.portfolio_view import hold_days, open_since  # noqa: E402

_IST = ZoneInfo("Asia/Kolkata")


def _ts(day, hour=10):
    return datetime(2026, 11, day, hour, 0, tzinfo=_IST).timestamp()


def test_open_since_uses_the_last_rebuy_after_a_full_exit():
    trades = [
        {"symbol": "ABC", "exchange": "NSE", "action": "BUY", "quantity": 10, "timestamp": _ts(2)},
        {"symbol": "ABC", "exchange": "NSE", "action": "SELL", "quantity": 10, "timestamp": _ts(3)},
        {"symbol": "ABC", "exchange": "NSE", "action": "BUY", "quantity": 5, "timestamp": _ts(10)},
        {"symbol": "XYZ", "exchange": "NSE", "action": "BUY", "quantity": 4, "timestamp": _ts(5)},
        {"symbol": "XYZ", "exchange": "NSE", "action": "SELL", "quantity": 1, "timestamp": _ts(6)},
    ]
    start = open_since(trades)
    assert start[("ABC", "NSE")] == _ts(10)
    assert start[("XYZ", "NSE")] == _ts(5)


def test_open_since_skips_flat_positions_and_bad_rows():
    trades = [
        {"symbol": "ABC", "exchange": "NSE", "action": "BUY", "quantity": 2, "timestamp": _ts(2)},
        {"symbol": "ABC", "exchange": "NSE", "action": "SELL", "quantity": 2, "timestamp": _ts(4)},
        {"symbol": "ABC", "exchange": "NSE", "action": "HOLD", "quantity": 2, "timestamp": _ts(4)},
        {"symbol": "ABC", "exchange": "NSE", "action": "BUY", "quantity": 2, "timestamp": "not a time"},
    ]
    assert open_since(trades) == {}
    assert open_since("not a list") == {}


def test_hold_days_counts_ist_calendar_days_and_handles_unknown():
    assert hold_days(_ts(2), _ts(20)) == 18
    assert hold_days(None, _ts(20)) is None
    assert hold_days(_ts(20, 9), _ts(20, 15)) == 0


def test_reader_fills_hold_days_and_survives_tradebook_failure():
    def fetch(key):
        return _reply(1)

    def trades_ok(key):
        return {"status": "success", "data": [
            {"symbol": "SYM0", "exchange": "NSE", "action": "BUY", "quantity": 10, "timestamp": _ts(2)}]}

    reader = HoldingsReader(fetch, lambda: "k", lambda: _ts(20), trades=trades_ok)
    assert reader.read()["holdings"][0]["hold_days"] == 18

    def trades_down(key):
        raise OSError("down")

    reader = HoldingsReader(fetch, lambda: "k", lambda: _ts(20), trades=trades_down)
    result = reader.read()
    assert result["state"] == "broker_read"
    assert result["holdings"][0]["hold_days"] is None
