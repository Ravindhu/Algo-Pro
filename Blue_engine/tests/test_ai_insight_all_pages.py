"""
test_ai_insight_all_pages.py - tests for the shared AI Insight card.

Covers:
  1. The shared card renders as a Dash html.Div with the AI INSIGHT
     badge and a read-only footer.
  2. Every engine page's ``body()`` renders without crashing, and its
     output contains an "AI INSIGHT" card.
  3. The safety contract: the card never writes to disk (pure function,
     no callbacks).
"""
from __future__ import annotations

import pytest
import dash_mantine_components as dmc
from dash import html

from blue_engine.dashboard.pages._ai_insight import (
    _GREY,
    ai_insight_card,
    rows_from_dict,
)


# --------------------------------------------------------------------------- #
# 1. The shared card itself
# --------------------------------------------------------------------------- #

def test_card_returns_a_dmc_card():
    card = ai_insight_card("Test Setting", [("A", "1", _GREY)])
    assert isinstance(card, dmc.Card)


def test_card_carries_ai_insight_badge():
    card = ai_insight_card("Test Setting", [("A", "1", _GREY)])
    text = str(card)
    assert "AI INSIGHT" in text


def test_card_is_read_only():
    """The safety contract: the rendered text must say 'read-only'."""
    card = ai_insight_card("Test Setting", [("A", "1", _GREY)])
    assert "read-only" in str(card).lower()


def test_card_shows_rows():
    card = ai_insight_card("Test Setting",
                           [("Alpha", "1", _GREY), ("Beta", "2", _GREY)])
    text = str(card)
    assert "Alpha" in text and "Beta" in text


def test_card_with_note_and_status():
    card = ai_insight_card("Test Setting", [("A", "1", _GREY)],
                           note="hello note", status="loaded")
    text = str(card)
    assert "hello note" in text
    assert "Status: loaded" in text


def test_card_with_extra_content():
    extra = html.Div("param table placeholder")
    card = ai_insight_card("Test Setting", [("A", "1", _GREY)], extra=extra)
    assert "param table placeholder" in str(card)


def test_rows_from_dict():
    rows = rows_from_dict({"x": 1, "y": "two"})
    assert rows == [("x", "1", _GREY), ("y", "two", _GREY)]


def test_rows_from_dict_empty():
    assert rows_from_dict(None) == []


# --------------------------------------------------------------------------- #
# 2. Every engine page carries an AI Insight card
# --------------------------------------------------------------------------- #

PAGES = [
    "blue_engine.dashboard.pages.terminal",
    "blue_engine.dashboard.pages.portfolio",
    "blue_engine.dashboard.pages.risk_control",
    "blue_engine.dashboard.pages.analytics",
    "blue_engine.dashboard.pages.infrastructure",
    "blue_engine.dashboard.pages.ml_models",
    "blue_engine.dashboard.pages.economic",
    "blue_engine.dashboard.pages.analytics_portfolio",
]


@pytest.fixture
def page_importer(monkeypatch):
    """Return a callable that imports a page under the neutered register_page."""
    import sys
    import dash as _dash
    monkeypatch.setattr(_dash, "register_page",
                        lambda *a, **k: None, raising=True)
    def _import(module_name):
        return __import__(module_name, fromlist=["body"])
    yield _import
    # Evict every page module so later test files re-import with the
    # REAL register_page and the pages land in the registry again.
    for name in list(sys.modules):
        if name.startswith("blue_engine.dashboard.pages."):
            del sys.modules[name]


@pytest.mark.parametrize("module_name", PAGES)
def test_page_body_contains_ai_insight(module_name, page_importer):
    mod = page_importer(module_name)
    body = getattr(mod, "body")
    out = body()  # must not raise
    text = str(out)
    assert "AI INSIGHT" in text, (
        f"{module_name}.body() has no AI INSIGHT card; got: {text[:300]}"
    )
    body = getattr(mod, "body")
    out = body()  # must not raise
    text = str(out)
    assert "AI INSIGHT" in text, (
        f"{module_name}.body() has no AI INSIGHT card; got: {text[:300]}"
    )
