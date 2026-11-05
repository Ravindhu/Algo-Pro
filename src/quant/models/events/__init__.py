"""Event scoring package. Logic lives in ``scoring.py``.

This file only re-exports names so ``quant.models.events`` keeps working.
Advice only: nothing here creates orders or sets size.
"""
from .scoring import (  # noqa: F401
    NEGATIVE,
    POSITIVE,
    EventAdvice,
    score_filing,
    score_filing_advice,
)

__all__ = ["EventAdvice", "NEGATIVE", "POSITIVE", "score_filing", "score_filing_advice"]
