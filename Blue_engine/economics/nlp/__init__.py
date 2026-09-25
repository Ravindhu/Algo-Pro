"""
nlp - text -> scores for the economic engine.

    sentiment        lexical (dictionary) tone, always on, no key
    llm_scorer       GPT / local-LLM structured scoring (opt-in, cached)
    rbi_minutes      RBI text -> risk-on tone (lexical now, LLM optional)
    event_calendar   scheduled event risk (budget / MPC / Fed / expiry)

Everything degrades: with no key and no network, ``sentiment`` and
``event_calendar`` still produce usable signals.
"""
from __future__ import annotations

from . import event_calendar, sentiment
from .sentiment import score_headlines, score_text

__all__ = [
    "sentiment", "score_text", "score_headlines",
    "event_calendar", "rbi_minutes", "llm_scorer",
]