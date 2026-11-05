"""Event scoring from filings. ADVICE ONLY: output never sets size or creates orders.

Flow stage: Event scoring
Status: PHASE 2 IMPLEMENTED (deterministic fallback added 2026-11-05, X-17)
Existing code to reuse or wrap (do not copy): Blue_engine/economics/nlp/event_calendar.py

WHY A FALLBACK
--------------
An LLM scorer can be passed in by the frontline caller. It is optional. If it
is missing, raises, or returns a non-number, the keyword scorer below is used.
Every result says which source made it (``advice_source``). Event output is an
input to a later deterministic stage, which may ignore it. No LLM client is
imported here.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

POSITIVE = {"order win": 1.0, "buyback": 0.5, "dividend": 0.3, "upgrade": 0.5, "record profit": 0.8}
NEGATIVE = {"fraud": -1.0, "default": -1.0, "sebi order": -0.7, "downgrade": -0.5, "profit warning": -0.6}

#: Signature of an optional LLM scorer: (symbol, text) -> score in [-1, 1].
LlmScorer = Callable[[str, str], float]


@dataclass(frozen=True)
class EventAdvice:
    symbol: str
    score: float                       # bounded to [-1, 1]
    matched: Tuple[str, ...] = field(default=())
    sets_size: bool = False            # fixed False: advice cannot size positions
    source: str = "deterministic"      # "deterministic" or "llm"

    @property
    def advice_score(self) -> float:
        """Same value as ``score``. The ``advice_`` name marks it as advice, not a directive."""
        return self.score

    @property
    def advice_source(self) -> str:
        return self.source


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def score_filing(symbol: str, text: str) -> EventAdvice:
    """Keyword scorer. Deterministic, no model, no network."""
    t = text.lower()
    hits, total = [], 0.0
    for table in (POSITIVE, NEGATIVE):
        for phrase, w in table.items():
            if phrase in t:
                hits.append(phrase)
                total += w
    return EventAdvice(symbol, _clamp(total), tuple(hits), source="deterministic")


def score_filing_advice(symbol: str, text: str,
                        llm_scorer: Optional[LlmScorer] = None) -> EventAdvice:
    """Use the LLM scorer if it works; otherwise fall back to the keyword scorer.

    Fallback happens when: no scorer is given, it raises, or it returns a value
    that is not a finite number. A failure is logged as WARNING, never raised.
    Scores from the LLM are clamped to [-1, 1].
    """
    if llm_scorer is None:
        return score_filing(symbol, text)
    try:
        raw = float(llm_scorer(symbol, text))
    except Exception as exc:  # noqa: BLE001 - any LLM failure falls back
        logger.warning("event LLM scorer failed for %s, using keyword fallback: %s", symbol, exc)
        return score_filing(symbol, text)
    if not math.isfinite(raw):
        logger.warning("event LLM scorer returned non-finite value for %s, using keyword fallback", symbol)
        return score_filing(symbol, text)
    return EventAdvice(symbol, _clamp(raw), (), source="llm")
