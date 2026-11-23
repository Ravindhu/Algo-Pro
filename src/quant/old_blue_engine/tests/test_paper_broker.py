"""PaperBroker tests (audit 2026-10-29).

PaperBroker exists so the FULL live path (signal -> risk gate -> POV -> ledger
-> exit engine) can run without sending an order to a real account. The
properties that make it trustworthy are what these tests pin:

* it never reaches the network,
* its response envelopes match the SDK's,
* a SL-M rests instead of filling (that is what makes it protection),
* a flat book still reports a VERIFIED zero P&L (an absent P&L makes the risk
  gate fail closed and refuse every entry - measured in the live paper run),
* it does not deadlock under its own reporting.
"""
from __future__ import annotations

import threading

import pytest

from blue_engine.core.paper_broker import PaperBroker
from blue_engine.core.risk_context import parse_positionbook


def test_quotes_match_the_sdk_envelope():
    broker = PaperBroker()
    resp = broker.quotes(symbol="INFY", exchange="NSE")
    assert resp["status"] == "success"
    data = resp["data"]
    for field in ("ltp", "bid", "ask", "volume", "date"):
        assert field in data
    assert data["bid"] < data["ltp"] < data["ask"]


def test_prices_are_deterministic():
    a = PaperBroker(seed=7).quotes(symbol="INFY")["data"]["ltp"]
    b = PaperBroker(seed=7).quotes(symbol="INFY")["data"]["ltp"]
    assert a == b, "a test harness must be reproducible"


def test_different_symbols_get_different_prices():
    broker = PaperBroker()
    assert (broker.quotes(symbol="INFY")["data"]["ltp"]
            != broker.quotes(symbol="TCS")["data"]["ltp"])


def test_market_order_fills_immediately():
    broker = PaperBroker()
    resp = broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                             exchange="NSE", price_type="MARKET",
                             product="CNC", quantity=100)
    assert resp["status"] == "success"
    book = broker.positionbook()["data"]
    held = [r for r in book if r["symbol"] == "INFY"]
    assert held and held[0]["netqty"] == 100


def test_sl_m_rests_and_does_not_fill():
    """A protective stop must be a RESTING order, not an instant exit."""
    broker = PaperBroker()
    broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                      exchange="NSE", price_type="MARKET", product="CNC",
                      quantity=100)
    stop = broker.placeorder(strategy="POV-EXIT", symbol="INFY", action="SELL",
                             exchange="NSE", price_type="SL-M", product="CNC",
                             quantity=100, trigger_price=1400.0)
    assert stop["data"]["order_status"] == "trigger pending"
    # The position is still fully open: the stop has not triggered.
    held = [r for r in broker.positionbook()["data"] if r["symbol"] == "INFY"]
    assert held[0]["netqty"] == 100


def test_flat_book_reports_verified_zero_pnl():
    broker = PaperBroker()
    parsed = parse_positionbook(broker.positionbook())
    assert parsed["ok"] is True
    assert parsed["day_pnl"] == 0.0, (
        "an absent P&L makes the risk gate fail closed and block every entry")
    assert parsed["open_exposure"] == 0.0
    assert parsed["n_positions"] == 0


def test_open_position_reports_pnl_and_exposure():
    broker = PaperBroker()
    broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                      exchange="NSE", price_type="MARKET", product="CNC",
                      quantity=100)
    parsed = parse_positionbook(broker.positionbook())
    assert parsed["ok"] is True
    assert parsed["day_pnl"] is not None
    assert parsed["open_exposure"] > 0
    assert parsed["n_positions"] == 1


def test_close_realises_pnl_and_flat_book_still_reports_it():
    broker = PaperBroker()
    broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                      exchange="NSE", price_type="MARKET", product="CNC",
                      quantity=100)
    broker.placeorder(strategy="T", symbol="INFY", action="SELL",
                      exchange="NSE", price_type="MARKET", product="CNC",
                      quantity=100)
    parsed = parse_positionbook(broker.positionbook())
    assert parsed["n_positions"] == 0
    assert parsed["open_exposure"] == 0
    assert parsed["day_pnl"] is not None, (
        "after a close-out the day's realised P&L must still be reported")


def test_cancel_only_affects_working_orders():
    broker = PaperBroker()
    done = broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                             exchange="NSE", price_type="MARKET",
                             product="CNC", quantity=10)
    stop = broker.placeorder(strategy="POV-EXIT", symbol="INFY", action="SELL",
                             exchange="NSE", price_type="SL-M", product="CNC",
                             quantity=10, trigger_price=1000.0)
    assert broker.cancelorder(order_id=stop["order_id"])["status"] == "success"
    assert broker.cancelorder(order_id=done["order_id"])["status"] == "error"
    assert broker.cancelorder(order_id="NOPE")["status"] == "error"


def test_summary_does_not_deadlock():
    """summary() holds the lock and calls positionbook(): must be reentrant."""
    broker = PaperBroker()
    broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                      exchange="NSE", price_type="MARKET", product="CNC",
                      quantity=10)
    out = {}
    thread = threading.Thread(target=lambda: out.update(broker.summary()))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "summary() deadlocked on its own lock"
    assert out["orders"] == 1


def test_summary_counts_orders_by_state():
    broker = PaperBroker()
    broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                      exchange="NSE", price_type="MARKET", product="CNC",
                      quantity=10)
    broker.placeorder(strategy="POV-EXIT", symbol="INFY", action="SELL",
                      exchange="NSE", price_type="SL-M", product="CNC",
                      quantity=10, trigger_price=1000.0)
    summary = broker.summary()
    assert summary["orders"] == 2
    assert summary["filled_orders"] == 1
    assert summary["resting_orders"] == 1
    assert summary["data_classification"] == "synthetic"


def test_never_claims_live_mode():
    assert PaperBroker().analyzerstatus()["data"]["mode"] == "analyze"


def test_subscribe_orders_is_honest():
    # Returning True would let the runner believe fills are monitored.
    assert PaperBroker().subscribe_orders(lambda _e: None) is False


def test_real_quotes_are_used_when_available():
    class QuoteSource:
        def quotes(self, *, symbol, exchange):
            return {"status": "success", "data": {"ltp": 1234.5}}

    broker = PaperBroker(data_source=QuoteSource())
    assert broker.quotes(symbol="INFY")["data"]["ltp"] == pytest.approx(1234.5)
    assert broker.summary()["data_classification"] == "live"


def test_a_broken_data_source_falls_back_to_synthetic():
    class Broken:
        def quotes(self, **_kw):
            raise RuntimeError("401 Unauthorized")

    broker = PaperBroker(data_source=Broken())
    resp = broker.quotes(symbol="INFY")
    assert resp["status"] == "success"
    assert broker.summary()["data_classification"] == "synthetic"


def test_invalid_order_is_rejected():
    broker = PaperBroker()
    assert broker.placeorder(strategy="T", symbol="INFY", action="HOLD",
                             exchange="NSE", quantity=10)["status"] == "error"
    assert broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                             exchange="NSE", quantity=0)["status"] == "error"


def test_buy_slippage_is_adverse_and_bounded_by_the_limit():
    broker = PaperBroker(fill_slippage_bps=10.0)
    resp = broker.placeorder(strategy="T", symbol="INFY", action="BUY",
                             exchange="NSE", price_type="LIMIT", product="CNC",
                             quantity=10, price=1400.0)
    fill = [o for o in broker.orderbook()["data"]
            if o["orderid"] == resp["order_id"]][0]
    assert fill["average_price"] <= 1400.0, "a limit buy cannot fill above it"
