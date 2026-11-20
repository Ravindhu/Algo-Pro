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


def test_trade_costs_price_cnc_as_delivery_and_mis_as_intraday():
    from quant.research.costs import CostModel
    from quant.reporting.portfolio_view import trade_costs
    model = CostModel()
    trades = [
        {"symbol": "ABC", "action": "BUY", "quantity": 10, "averageprice": 100.0, "product": "CNC"},
        {"symbol": "ABC", "action": "SELL", "quantity": 10, "averageprice": 110.0, "product": "MIS"},
        {"symbol": "BAD", "action": "BUY", "quantity": None, "averageprice": 5.0},
    ]
    out = trade_costs(trades, model)
    expected = (model.trade_cost(1000.0, "BUY", "delivery") + model.trade_cost(1100.0, "SELL", "intraday"))
    assert out["fills"] == 2 and out["skipped"] == 1
    assert abs(out["inr"] - round(expected, 2)) < 0.01
    assert trade_costs(None)["fills"] == 0


def test_reader_reports_trade_cost_as_pct_of_invested():
    def fetch(key):
        return _reply(1)

    def trades_ok(key):
        return {"status": "success", "data": [
            {"symbol": "SYM0", "exchange": "NSE", "action": "BUY", "quantity": 10,
             "averageprice": 100.0, "product": "CNC", "timestamp": 1_700_000_000}]}

    out = HoldingsReader(fetch, lambda: "k", lambda: 1_700_100_000.0, trades=trades_ok).read()
    tc = out["trade_cost"]
    assert tc["fills"] == 1 and tc["inr"] > 0 and tc["tax_inr"] == 0
    assert abs(tc["pct_of_invested"] - tc["total_inr"] / 2000.0 * 100) < 0.001


def test_capital_gains_tax_nets_losses_and_applies_ltcg_exemption():
    from quant.research.costs import CostModel
    m = CostModel()
    # Short-term loss offsets short-term gain; long-term gain below exemption is untaxed.
    out = m.capital_gains_tax(short_term_gain=10_000.0, long_term_gain=100_000.0)
    assert out["ltcg_inr"] == 0.0
    assert abs(out["stcg_inr"] - 10_000 * 0.20 * 1.04) < 0.01
    # Net loss owes nothing.
    assert m.capital_gains_tax(-5_000.0, -1.0)["tax_inr"] == 0.0
    # LTCG above exemption is taxed at 12.5% plus cess.
    lt = m.capital_gains_tax(0.0, 225_000.0)
    assert abs(lt["ltcg_inr"] - 100_000 * 0.125 * 1.04) < 0.01


def test_delivery_gains_fifo_splits_short_and_long_term_and_excludes_intraday():
    from quant.reporting.portfolio_view import delivery_gains
    day = 86400
    trades = [
        {"symbol": "A", "exchange": "NSE", "action": "BUY", "quantity": 10, "averageprice": 100,
         "product": "CNC", "timestamp": 0},
        {"symbol": "A", "exchange": "NSE", "action": "BUY", "quantity": 10, "averageprice": 200,
         "product": "CNC", "timestamp": 400 * day},
        # Sells 10 from the first lot (held 400 days -> long term) and 5 from the second (short term).
        {"symbol": "A", "exchange": "NSE", "action": "SELL", "quantity": 15, "averageprice": 300,
         "product": "CNC", "timestamp": 401 * day},
        # Intraday round trip is excluded from capital gains.
        {"symbol": "B", "exchange": "NSE", "action": "BUY", "quantity": 1, "averageprice": 10,
         "product": "MIS", "timestamp": 401 * day},
        {"symbol": "B", "exchange": "NSE", "action": "SELL", "quantity": 1, "averageprice": 50,
         "product": "MIS", "timestamp": 401 * day + 60},
        # Sell with no buy in the list: unmatched, not taxed.
        {"symbol": "C", "exchange": "NSE", "action": "SELL", "quantity": 4, "averageprice": 80,
         "product": "CNC", "timestamp": 401 * day},
    ]
    g = delivery_gains(trades)
    assert g["long_term"] == (300 - 100) * 10          # 2000
    assert g["short_term"] == (300 - 200) * 5          # 500
    assert g["matched_qty"] == 15 and g["unmatched_qty"] == 4
    assert g["intraday_excluded"] == 2


def test_mtf_interest_is_12_percent_a_year_on_a_daily_base():
    from quant.research.costs import CostModel
    m = CostModel()
    assert abs(m.mtf_daily_interest(365_000.0) - 120.0) < 1e-9  # 365,000 x 12% / 365


def test_apply_margin_adds_mtf_pct_and_interest_to_cost_of_trading():
    from quant.reporting.portfolio_view import apply_margin
    payload = {"summary": {"value": 200_000.0, "invested": 100_000.0},
               "trade_cost": {"inr": 100.0, "tax_inr": 0.0, "total_inr": 100.0, "pct_of_invested": 0.1}}
    out = apply_margin(payload, 50_000.0)
    assert out["mtf"]["mtf_pct"] == 25.0
    assert out["mtf"]["daily_interest_inr"] == round(50_000 * 0.12 / 365, 2)
    tc = out["trade_cost"]
    assert tc["total_inr"] == round(100.0 + tc["interest_inr"], 2)
    assert abs(tc["pct_of_invested"] - tc["total_inr"] / 100_000 * 100) < 1e-3


def test_apply_margin_without_tradebook_still_charges_interest():
    from quant.reporting.portfolio_view import apply_margin
    out = apply_margin({"summary": {"value": 10.0, "invested": 10.0}, "trade_cost": None}, 1_000.0)
    assert out["trade_cost"]["fills"] == 0 and out["trade_cost"]["total_inr"] > 0
    none = apply_margin({"summary": {"value": 10.0, "invested": 10.0}, "trade_cost": None}, 0.0)
    assert none["trade_cost"] is None and none["mtf"]["mtf_pct"] == 0.0


def test_setup_store_saves_and_validates_mtf_borrowed(tmp_path):
    from quant.core.setup_store import apply_setup_updates, load_mtf_borrowed, read_setup
    settings = tmp_path / "settings.yaml"
    limits = tmp_path / "risk_limits.yaml"
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    limits.write_text("max_symbols_per_portfolio: 5\n", encoding="utf-8")
    apply_setup_updates(settings, strategies, limits, {"mtf_borrowed_inr": 250000})
    assert load_mtf_borrowed(settings) == 250000.0
    assert read_setup(settings, strategies, limits)["mtf_borrowed_inr"] == 250000.0
    import pytest
    with pytest.raises(ValueError):
        apply_setup_updates(settings, strategies, limits, {"mtf_borrowed_inr": -1})
    assert load_mtf_borrowed(tmp_path / "missing.yaml") == 0.0


def test_mstock_fund_summary_parses_mtf_fields_and_prefers_eq_row():
    from quant.execution.mstock_funds import parse_fund_summary
    body = {"status": "success", "data": [
        {"SEG": "CO", "MTF_UTILIZE": "1"},
        {"SEG": "EQ", "MTF_UTILIZE": "250,000.50", "MTF_COLLATERAL": 300000, "MTF_AVAILABLE_BALANCE": "10",
         "AVAILABLE_BALANCE": 5000},
    ]}
    out = parse_fund_summary(body)
    assert out["mtf_utilize"] == 250000.5 and out["mtf_collateral"] == 300000.0
    assert out["available_balance"] == 5000.0


def test_mstock_fetch_is_get_with_documented_headers_and_never_raises():
    from quant.execution.mstock_funds import fetch_fund_summary

    class Resp:
        status_code = 200
        def json(self):
            return {"status": "success", "data": [{"SEG": "EQ", "MTF_UTILIZE": 100}]}

    seen = {}
    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return Resp()
    out = fetch_fund_summary("KEY", "JWT", get=fake_get)
    assert out["state"] == "ok" and out["mtf"]["mtf_utilize"] == 100.0
    assert seen["url"].endswith("/openapi/typeb/user/fundsummary")
    assert seen["headers"]["Authorization"] == "token KEY:JWT"
    assert seen["headers"]["X-Mirae-Version"] == "1"

    def boom(*a, **k):
        raise OSError("no route")
    assert fetch_fund_summary("K", "T", get=boom)["state"] == "unavailable"


def test_read_mtf_reports_not_configured_without_env_and_caches():
    import quant.execution.mstock_funds as mf
    mf._cache.update(at=None, value=None)
    assert mf.read_mtf(environ={})["state"] == "not_configured"
    calls = []
    class Resp:
        status_code = 200
        def json(self):
            return {"status": "success", "data": [{"SEG": "EQ", "MTF_UTILIZE": 7}]}
    def get(*a, **k):
        calls.append(1)
        return Resp()
    env = {"MSTOCK_API_KEY": "K", "MSTOCK_ACCESS_TOKEN": "T"}
    first = mf.read_mtf(get=get, clock=lambda: 1000.0, environ=env)
    second = mf.read_mtf(get=get, clock=lambda: 1010.0, environ=env)
    assert first["state"] == "ok" and second is first and len(calls) == 1
    mf._cache.update(at=None, value=None)


def test_apply_margin_records_source_for_the_page():
    from quant.reporting.portfolio_view import apply_margin
    out = apply_margin({"summary": {"value": 100.0, "invested": 100.0}, "trade_cost": None},
                       500.0, source="mstock")
    assert out["mtf"]["source"] == "mstock" and out["mtf"]["mtf_pct"] == 500.0
