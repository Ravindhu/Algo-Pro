"""Slice 1 of the bot exchange carry (docs/IMPLEMENTATION_PLAN_2026-11-25_BOT_EXCHANGE_CARRY.md).

The intent names its exchange. Entry orders send that exchange. Default stays NSE.
"""
import pytest

from quant.core.types import GateDecision, TradeIntent
from quant.execution.openalgo_client import OpenAlgoClient
from test_analyze_mode import ON, PLACED, Fake

APPROVED = GateDecision("I1", True, ())


def _intent(exchange=None, intent_id="I1"):
    kwargs = {"exchange": exchange} if exchange is not None else {}
    return TradeIntent(intent_id=intent_id, strategy_id="S1", symbol="RELIANCE", side="BUY",
                       qty=10, ref_price=1500.0, kind="entry", lot_size=1, **kwargs)


def _client(fake):
    return OpenAlgoClient("http://x", fake, mode="analyze", exchange="NSE")


def test_default_intent_is_nse():
    assert _intent().exchange == "NSE"


def test_default_intent_sends_nse_on_place():
    fake = Fake(analyzer=ON, place=PLACED)
    _client(fake).place(_intent(), "key", APPROVED)
    sent = fake.sent()
    assert sent and sent[-1][1]["exchange"] == "NSE"


def test_bse_intent_sends_bse_on_place():
    fake = Fake(analyzer=ON, place=PLACED)
    _client(fake).place(_intent("BSE"), "key", APPROVED)
    assert fake.sent()[-1][1]["exchange"] == "BSE"


def test_bse_intent_sends_bse_on_place_child_market_and_limit():
    fake = Fake(analyzer=ON, place=PLACED)
    client = _client(fake)
    client.place_child(_intent("BSE"), "key", APPROVED, price_type="MARKET")
    client.place_child(_intent("BSE", intent_id="I2"), "key",
                       GateDecision("I2", True, ()), price_type="LIMIT", limit_price=1500.5)
    exchanges = [payload["exchange"] for _, payload in fake.sent()]
    assert exchanges == ["BSE", "BSE"]


def test_unknown_exchange_is_refused_when_the_intent_is_built():
    with pytest.raises(ValueError):
        _intent("MCX")
    with pytest.raises(ValueError):
        _intent("nse")  # case-sensitive: only the two exact codes


def test_client_exchange_is_only_a_default_not_the_order_exchange():
    # The client is built for NSE, but a BSE intent must still go to BSE.
    fake = Fake(analyzer=ON, place=PLACED)
    _client(fake).place(_intent("BSE"), "key", APPROVED)
    assert fake.sent()[-1][1]["exchange"] == "BSE"
