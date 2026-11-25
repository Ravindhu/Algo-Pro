"""Read-only broker margin call (owner 2026-11-25): one read to OpenAlgo /margin, nothing placed.

Flow stage: Adapters (OpenAlgo read path)
Status: PHASE 2 IMPLEMENTED
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402


def _client(reply=None):
    sent = []

    def transport(url, payload):
        sent.append((url, payload))
        return reply or {"status": "success", "data": {"total_margin_required": 20000.0}}
    return OpenAlgoReadOnly("http://oa.local/", transport), sent


def test_margin_for_order_posts_one_position_to_margin_path():
    client, sent = _client()
    client.margin_for_order("KEY", "INFY", "NSE", "BUY", 100, "MTF", "MARKET", 1000.0)
    url, payload = sent[0]
    assert url == "http://oa.local/api/v1/margin"
    assert payload["apikey"] == "KEY"
    assert payload["positions"] == [{"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                                     "quantity": "100", "product": "MTF", "pricetype": "MARKET",
                                     "price": "1000.0"}]


def test_margin_for_order_returns_broker_reply_unchanged():
    reply = {"status": "success", "data": {"total_margin_required": 20000.0}}
    client, _ = _client(reply)
    assert client.margin_for_order("KEY", "INFY", "NSE", "BUY", 100, "MTF") == reply


def test_margin_for_order_needs_a_key():
    client, sent = _client()
    with pytest.raises(ValueError):
        client.margin_for_order("", "INFY", "NSE", "BUY", 100, "MTF")
    assert sent == []


def test_read_client_has_no_order_methods():
    for name in ("place", "place_child", "place_stop", "cancel", "modify", "close"):
        assert not hasattr(OpenAlgoReadOnly, name)
