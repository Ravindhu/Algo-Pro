"""POV sizing must fail closed (audit 2026-11-03, fix 2).

Weights that cannot be priced must refuse the order, never trade uncapped. A
price is used only when its source names the executed symbol.
"""
from __future__ import annotations

from blue_engine.pov.pov_engine import PovEngine

BOOK = {"capital": 1_000_000.0, "target_weights": {"INFY": 0.05}}


def _slice(context, quantity=1000):
    engine = PovEngine({"pov": {"quantity": quantity}})
    sliced, _ = engine._slice(context, [])
    return sliced


def test_weights_without_any_price_refuse_the_order():
    sliced = _slice({"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                     "quantity": 1000, "portfolio": BOOK})
    assert sliced["status"] == "skipped"
    assert sliced["reason"] == "sizing_unverified"


def test_same_symbol_context_price_caps_to_the_sleeve():
    sliced = _slice({"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                     "quantity": 1000, "ltp": 1000.0, "portfolio": BOOK})
    assert sliced["config"]["quantity"] == 50  # 5% of 10 lakh at Rs 1000


def test_book_price_for_another_symbol_is_not_used():
    book = dict(BOOK, price=50.0, symbol="TCS")
    sliced = _slice({"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                     "quantity": 1000, "portfolio": book})
    assert sliced["reason"] == "sizing_unverified"


def test_book_price_naming_the_same_symbol_is_accepted():
    book = dict(BOOK, price=1000.0, symbol="INFY")
    sliced = _slice({"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                     "quantity": 1000, "portfolio": book})
    assert sliced["config"]["quantity"] == 50


def test_no_weights_means_no_sizing_is_requested():
    sliced = _slice({"symbol": "INFY", "exchange": "NSE", "action": "BUY",
                     "quantity": 300, "portfolio": {"capital": 1_000_000.0}})
    assert sliced["config"]["quantity"] == 300
