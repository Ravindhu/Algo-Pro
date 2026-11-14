"""Five planned Blue wraps: each one fails closed and never raises.

Flow stage: Event calendar, Economic providers, LLM token governor, Broker auth, Lot accounting
Status: TEST

Every section checks the pass path and a block path:
  * event calendar  - a major event blocks entries; expiry does not; a failure blocks
  * providers       - a raising or meta-less provider is not ok; missing freshness is stale
  * token governor  - an over-budget prompt is flagged; a failure returns None (not sent)
  * broker auth     - the wrapper never calls a login; a failure reports not allowed
  * lot accounting  - an unknown lot size sends nothing (Blue would pass it through)
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

from quant.data import economic_providers, event_calendar
from quant.execution import broker_auth_status, lot_size
from quant.models.events import token_budget

AS_OF = dt.date(2026, 11, 14)  # a Saturday


# ----------------------------------------------------------------- event calendar
def test_major_event_inside_window_blocks_entries():
    res = event_calendar.event_window(AS_OF, fixed_events=["2026-11-15"], include_expiry=False)
    assert res["ok"] is True
    assert res["blocked"] is True
    assert res["reason"]


def test_major_event_outside_window_does_not_block():
    res = event_calendar.event_window(AS_OF, fixed_events=["2026-12-20"], include_expiry=False)
    assert res["ok"] is True
    assert res["blocked"] is False


def test_monthly_expiry_is_reported_but_does_not_block():
    res = event_calendar.event_window(AS_OF, within_days=14, include_expiry=True)
    assert res["ok"] is True
    assert any(e["name"] == "FNO_EXPIRY" for e in res["events"])
    assert res["blocked"] is False


def test_calendar_failure_blocks_entries(monkeypatch):
    monkeypatch.setattr(event_calendar, "_upcoming", lambda: None)
    res = event_calendar.event_window(AS_OF)
    assert res["ok"] is False and res["blocked"] is True


def test_calendar_raising_blocks_entries(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad config")
    monkeypatch.setattr(event_calendar, "_upcoming", lambda: boom)
    res = event_calendar.event_window(AS_OF)
    assert res["ok"] is False and res["blocked"] is True


# ----------------------------------------------------------------- economic providers
class _Provider:
    def __init__(self, payload=None, exc=None):
        self._payload, self._exc = payload, exc

    def fetch(self):
        if self._exc:
            raise self._exc
        return self._payload


def test_good_provider_is_ok_and_fresh():
    res = economic_providers.read_macro(_Provider({
        "gdp": {"value": 6.5}, "meta": {"ok": True, "stale": False, "warnings": []}}))
    assert res["ok"] is True and res["stale"] is False
    assert res["data"] == {"gdp": {"value": 6.5}}


def test_raising_provider_is_not_ok():
    res = economic_providers.read_macro(_Provider(exc=RuntimeError("feed down")))
    assert res["ok"] is False and res["stale"] is True


def test_provider_without_meta_is_untrusted():
    res = economic_providers.read_macro(_Provider({"gdp": 6.5}))
    assert res["ok"] is False


def test_missing_freshness_counts_as_stale():
    res = economic_providers.read_macro(_Provider({"meta": {"ok": True}}))
    assert res["ok"] is True and res["stale"] is True


def test_no_provider_supplied_is_not_ok():
    assert economic_providers.read_macro(None)["ok"] is False


def test_value_block_uses_blue_contract():
    block = economic_providers.to_block(6.5, previous=6.1, unit="pct", as_of="2026-11-01", source="test")
    assert block["value"] == 6.5 and block["previous"] == 6.1 and block["unit"] == "pct"


# ----------------------------------------------------------------- token governor
def test_small_prompt_is_within_budget():
    res = token_budget.budget("short prompt", max_tokens=1000)
    assert res["ok"] is True and res["within"] is True


def test_large_prompt_is_over_budget():
    res = token_budget.budget("x" * 100_000, max_tokens=100)
    assert res["ok"] is True and res["within"] is False and res["reason"]


def test_compact_shortens_long_text():
    out = token_budget.compact("abc " * 5000, max_chars=500)
    assert isinstance(out, str)
    assert len(out) <= 600  # the limit plus the elision marker
    assert "elided" in out


def test_cache_key_is_stable_and_model_sensitive():
    msgs = [{"role": "user", "content": "hello"}]
    a = token_budget.cache_key(msgs, model="m1")
    assert a is not None and a == token_budget.cache_key(msgs, model="m1")
    assert a != token_budget.cache_key(msgs, model="m2")


def test_token_failure_returns_none_not_a_prompt(monkeypatch):
    monkeypatch.setattr(token_budget, "_tokens", lambda: None)
    assert token_budget.budget("x", 10)["within"] is False
    assert token_budget.compact("x", 10) is None
    assert token_budget.cache_key([], model="m") is None


# ----------------------------------------------------------------- broker auth
def test_broker_auth_module_never_calls_a_login():
    """Static guard: the status wrapper must not call any login path."""
    path = pathlib.Path(broker_auth_status.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"run_scheduled_login", "BrokerAuth", "login", "OpenAlgoSession"}
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    assert not (called & forbidden), called & forbidden


def test_login_status_reads_without_error():
    res = broker_auth_status.login_status(now=0.0)
    assert res["ok"] is True
    assert isinstance(res["autologin_enabled"], bool)
    assert isinstance(res["allowed"], bool)


def test_broker_auth_failure_is_not_allowed(monkeypatch):
    def missing(name):
        from quant.core.blue_bridge import BlueEngineUnavailable
        raise BlueEngineUnavailable("gone")
    monkeypatch.setattr(broker_auth_status, "load", missing)
    res = broker_auth_status.login_status()
    assert res["ok"] is False and res["allowed"] is False and res["autologin_enabled"] is False


# ----------------------------------------------------------------- lot accounting
def test_lot_rounds_down_to_whole_lots():
    assert lot_size.floor_to_lot(2500, 1200) == {"ok": True, "qty": 2400, "lot_size": 1200, "reason": ""}
    assert lot_size.floor_to_lot(1200, 1200)["qty"] == 1200


def test_below_one_lot_sends_nothing_but_is_ok():
    res = lot_size.floor_to_lot(1199, 1200)
    assert res["ok"] is True and res["qty"] == 0


@pytest.mark.parametrize("bad_lot", [None, 0, -5, True, 12.5, "1200"])
def test_unknown_lot_size_sends_nothing(bad_lot):
    res = lot_size.floor_to_lot(1500, bad_lot)
    assert res["ok"] is False and res["qty"] == 0


def test_zero_lot_is_not_passed_through():
    """Regression: Blue's lot_quantity returns the input unchanged for lot 0."""
    assert lot_size.floor_to_lot(1500, 0)["qty"] == 0


@pytest.mark.parametrize("bad_qty", [2500.0, "2500", True])
def test_non_integer_quantity_sends_nothing(bad_qty):
    assert lot_size.floor_to_lot(bad_qty, 1200)["qty"] == 0


def test_lot_module_failure_sends_nothing(monkeypatch):
    from quant.core.blue_bridge import BlueEngineUnavailable

    def missing(name):
        raise BlueEngineUnavailable("gone")
    monkeypatch.setattr(lot_size, "load", missing)
    res = lot_size.floor_to_lot(2500, 1200)
    assert res["ok"] is False and res["qty"] == 0
