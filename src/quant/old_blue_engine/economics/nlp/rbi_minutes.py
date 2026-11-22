"""
nlp/rbi_minutes.py - RBI text -> risk-on signal (lexical now, LLM optional).

Bridges central-bank language to the engine's score space. It:

  1. always computes a **lexical** tone from the text (no key), and
  2. when an LLM is enabled, upgrades to a structured hawkish/dovish read.

The output is a single ``rbi_tone`` in [-1, +1] (positive = risk-on), which the
engine adds as a signal. The mapping is explicit so the desk can audit it:
hawkish language is risk-OFF (tighter policy -> lower multiples), dovish is
risk-ON.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from . import sentiment
from .llm_scorer import LLMScorer


def rbi_tone(text: str, *, llm: Optional[LLMScorer] = None,
             log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Score RBI text into a risk-on tone in [-1, +1]. Never raises.

    Returns::

        {"tone": float[-1,1], "method": "llm"|"lexical", "detail": {...}}
    """
    lexical = sentiment.score_text(text)
    if llm is not None and llm.enabled():
        res = llm.score(text)
        if res.get("method") in ("llm", "cached"):
            # hawkish in [-1,1] -> risk-on tone is its negative, scaled by
            # confidence so a low-confidence read is pulled toward zero.
            hawk = float(res.get("hawkish", 0.0))
            conf = float(res.get("confidence", 0.0))
            tone = max(-1.0, min(1.0, -hawk * conf))
            return {"tone": round(tone, 4), "method": "llm",
                    "detail": res}
        if log:
            log(f"[rbi_minutes] LLM unavailable ({res.get('method')}); "
                f"using lexical")
    return {"tone": float(lexical["score"]), "method": "lexical",
            "detail": lexical}


#: Simple explicit policy-action map (negative = tightening = risk-off).
POLICY_ACTION_TONE = {
    "rate_hike": -1.0, "rate_cut": 1.0, "hold": 0.0,
    "hike": -1.0, "cut": 1.0, "pause": 0.0,
    "hawkish": -0.8, "dovish": 0.8, "neutral": 0.0,
    "crr_hike": -0.7, "crr_cut": 0.7,
}


def policy_action_tone(action: str) -> float:
    """Map a discrete policy action label to a tone (auditable, explicit)."""
    key = (action or "").strip().lower().replace(" ", "_")
    return float(POLICY_ACTION_TONE.get(key, 0.0))


__all__ = ["rbi_tone", "policy_action_tone", "POLICY_ACTION_TONE"]