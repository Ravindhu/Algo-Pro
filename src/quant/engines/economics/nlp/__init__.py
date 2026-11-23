"""
nlp - text -> scores for the economic engine.

    sentiment        lexical (dictionary) tone, always on, no key
    event_calendar   scheduled event risk (budget / MPC / Fed / expiry)

Everything degrades: with no key and no network, ``sentiment`` and
``event_calendar`` still produce usable signals.

PARKED (not importable; kept in ``Temp/Stag/``):
    llm_scorer       superseded by ``core/llm/`` (router + protocol)
    rbi_minutes      superseded by ``sentiment.score_text`` (see
                     ``economic_engine._nlp_signals``)
Neither has a live caller, so they are deliberately absent from this package.
"""
from __future__ import annotations

from . import event_calendar, sentiment
from .sentiment import score_headlines, score_text

__all__ = [
    "sentiment", "score_text", "score_headlines",
    "event_calendar",
]