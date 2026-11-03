"""Event scoring from filings. ADVICE ONLY: output never sets size or creates orders.

Flow stage: Event scoring
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/economics/nlp/event_calendar.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

POSITIVE = {"order win": 1.0, "buyback": 0.5, "dividend": 0.3, "upgrade": 0.5, "record profit": 0.8}
NEGATIVE = {"fraud": -1.0, "default": -1.0, "sebi order": -0.7, "downgrade": -0.5, "profit warning": -0.6}


@dataclass(frozen=True)
class EventAdvice:
    symbol: str
    score: float                       # bounded to [-1, 1]
    matched: Tuple[str, ...] = field(default=())
    sets_size: bool = False            # fixed False: advice cannot size positions


def score_filing(symbol: str, text: str) -> EventAdvice:
    t = text.lower()
    hits, total = [], 0.0
    for table in (POSITIVE, NEGATIVE):
        for phrase, w in table.items():
            if phrase in t:
                hits.append(phrase)
                total += w
    return EventAdvice(symbol, max(-1.0, min(1.0, total)), tuple(hits))
