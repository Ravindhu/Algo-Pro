"""Wiring tests for the live path (audit 2026-10-29, P0-5 and the feed).

Two regressions are covered here:

1. ``subscribe_to_order_updates`` probed ``order_update_stream``, a method the
   installed SDK does not have, so it returned False on every live run and no
   fill was ever pushed into the ledger. The SDK's real API is
   ``client.subscribe_orders(on_order_update=callable)``.

2. ``volume_cache.subscribe`` streamed a hardcoded INFY/TCS/RELIANCE list with
   a fallback to INFY's token (``1594``). Any other symbol sliced on
   ``volume=0``. It is now driven by the caller's run symbols and refuses to
   guess a token.
"""
from __future__ import annotations

import blue_engine.core.order_updates as ou
import blue_engine.core.volume_cache as vc


# --------------------------------------------------------------------------- #
# 1. Order-update subscription
# --------------------------------------------------------------------------- #
class _FeedClient:
    def __init__(self, accept=True):
        self.accept = accept
        self.callback = None
        self.calls = 0

    def subscribe_orders(self, on_order_update=None):
        self.calls += 1
        self.callback = on_order_update
        return self.accept


def _reset_subscription(monkeypatch):
    monkeypatch.setattr(ou, "_SUBSCRIBED", False)
    # The OpenAlgo platform bus must not shadow the SDK path under test.
    import sys
    monkeypatch.setitem(sys.modules, "utils.event_bus", None)


def test_subscription_uses_the_sdk_subscribe_orders_api(monkeypatch):
    _reset_subscription(monkeypatch)
    client = _FeedClient()
    assert ou.subscribe_to_order_updates(client=client) is True
    assert client.calls == 1
    assert callable(client.callback), "the SDK must receive our handler"


def test_pushed_event_reaches_the_registered_callback(monkeypatch):
    _reset_subscription(monkeypatch)
    seen = []
    client = _FeedClient()
    ou.subscribe_to_order_updates(client=client, callback=seen.append)
    client.callback({"type": "order_update", "orderid": "O9",
                     "order_status": "Open"})
    assert seen and seen[0]["orderid"] == "O9"


def test_a_handler_error_does_not_kill_the_feed(monkeypatch):
    _reset_subscription(monkeypatch)
    client = _FeedClient()

    def _boom(_event):
        raise ValueError("handler failed")

    ou.subscribe_to_order_updates(client=client, callback=_boom)
    client.callback({"orderid": "O9"})       # must not raise
    client.callback({"orderid": "O10"})


def test_rejected_subscription_is_reported_not_swallowed(monkeypatch):
    _reset_subscription(monkeypatch)
    client = _FeedClient(accept=False)
    assert ou.subscribe_to_order_updates(client=client) is False
    # Not memoised as success: a later call retries.
    monkeypatch.setattr(ou, "_SUBSCRIBED", False)
    client.accept = True
    assert ou.subscribe_to_order_updates(client=client) is True


def test_client_without_subscribe_orders_degrades_explicitly(monkeypatch):
    _reset_subscription(monkeypatch)
    assert ou.subscribe_to_order_updates(client=object()) is False


def test_no_client_at_all():
    assert ou.subscribe_to_order_updates(client=None) is False


# --------------------------------------------------------------------------- #
# 2. Volume-cache symbol resolution
# --------------------------------------------------------------------------- #
class _TokenDB:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, symbol, exchange):
        return self.mapping.get((symbol, exchange))


def test_tokens_come_from_the_run_symbols_only(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("database.token_db")
    fake.token_db = _TokenDB({("TCS", "NSE"): 11536, ("BSEONLY", "BSE"): 501})
    monkeypatch.setitem(sys.modules, "database", types.ModuleType("database"))
    monkeypatch.setitem(sys.modules, "database.token_db", fake)


    resolved = vc._resolve_run_symbols([("TCS", "NSE"), "BSEONLY", ("UNKNOWN", "NSE")])
    # Only genuinely resolved tokens are returned; a bare symbol uses NSE and
    # an unknown one is omitted, never substituted.
    # ``BSEONLY`` is a bare (str) symbol, so it is looked up on NSE and simply
    # omitted - no token is invented for it.
    assert resolved == {"TCS_NSE": "11536"}
    assert not any(key.startswith("INFY") for key in resolved)


def test_bare_symbol_defaults_to_nse(monkeypatch):
    import sys
    import types
    fake = types.ModuleType("database.token_db")
    fake.token_db = _TokenDB({("INFY", "NSE"): 1594})
    monkeypatch.setitem(sys.modules, "database", types.ModuleType("database"))
    monkeypatch.setitem(sys.modules, "database.token_db", fake)
    assert vc._resolve_run_symbols(["INFY"]) == {"INFY_NSE": "1594"}


def test_unresolvable_symbols_produce_no_guessed_token(monkeypatch):
    import sys
    import types
    fake = types.ModuleType("database.token_db")
    fake.token_db = _TokenDB({})
    monkeypatch.setitem(sys.modules, "database", types.ModuleType("database"))
    monkeypatch.setitem(sys.modules, "database.token_db", fake)
    assert vc._resolve_run_symbols([("RELIANCE", "NSE")]) == {}


def test_missing_token_db_is_not_fatal(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "database.token_db", None)
    assert vc._resolve_run_symbols([("INFY", "NSE")]) == {}


def test_subscribe_declines_when_no_symbol_resolves(monkeypatch):
    """The WS must not start pointing at somebody else's tokens."""
    monkeypatch.setattr(vc, "_SUBSCRIBED", False)
    monkeypatch.setattr(vc, "_WS_RUNNING", False)
    monkeypatch.setattr(vc, "_ws_connect_and_subscribe",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("socket must not start")))
    monkeypatch.setattr(vc, "_resolve_run_symbols", lambda symbols: {})
    import sys, types
    auth = types.ModuleType("database.auth_db")
    auth.get_auth_token_broker = lambda key: ("token", "broker")
    monkeypatch.setitem(sys.modules, "database.auth_db", auth)
    monkeypatch.setenv("OPENALGO_API_KEY", "k")
    monkeypatch.setenv("BROKER_API_KEY", "b")
    assert vc.subscribe(client=object(), symbols=[("RELIANCE", "NSE")]) is False
