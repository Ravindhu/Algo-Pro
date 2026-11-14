"""
nlp/sentiment.py - lexical (dictionary-based) market sentiment. No key needed.

Always-on text scoring. Given headlines / central-bank sentences, returns a
signed tone in [-1, +1] using a small finance-tuned lexicon plus negation
handling. This is deliberately *simple and transparent*: every score is
explainable by the words that produced it, which is what a desk wants before it
trusts an LLM.

For richer scoring see :mod:`.llm_scorer` (opt-in, GPT/local LLM). The lexical
score is also the *fallback* when no LLM is configured, so the pipeline always
has a news signal.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

#: Finance-flavoured lexicon. Weights in [-1, +1]. Extend freely; the engine
#: reads only the final score, so the list can grow without code changes.
_POSITIVE = {
    "rally": 1.0, "surge": 1.0, "gain": 0.7, "rise": 0.6, "up": 0.4,
    "record high": 1.0, "outperform": 0.8, "upgrade": 0.8, "beats": 0.8,
    "optimism": 0.8, "boost": 0.7, "recovery": 0.7, "dovish": 0.7,
    "stimulus": 0.8, "easing": 0.6, "strong": 0.7, "growth": 0.6,
    "inflow": 0.7, "supportive": 0.5, "resilient": 0.6, "rate cut": 0.9,
}
_NEGATIVE = {
    "fall": -0.6, "drop": -0.7, "plunge": -1.0, "crash": -1.0, "slump": -0.9,
    "loss": -0.7, "down": -0.4, "sell-off": -1.0, "underperform": -0.8,
    "downgrade": -0.8, "miss": -0.8, "hawkish": -0.7, "inflation": -0.5,
    "recession": -1.0, "tightening": -0.7, "rate hike": -0.9, "default": -1.0,
    "outflow": -0.7, "weak": -0.7, "fear": -0.9, "crisis": -1.0,
    "volatility": -0.4, "warning": -0.6, "risk-off": -0.8, "contraction": -0.8,
}
_NEGATORS = {"not", "no", "never", "without", "fails", "unlikely", "denies"}


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9'-]+", (text or "").lower())


def score_text(text: str) -> Dict[str, Any]:
    """Score one text. Returns score in [-1,1] plus the contributing terms.

    Multi-word phrases (e.g. "rate cut", "record high") are matched first, then
    single tokens. A negator within two words flips a term's sign.
    """
    low = (text or "").lower()
    hits: List[Dict[str, Any]] = []

    # Phrase pass.
    for phrase, w in {**_POSITIVE, **_NEGATIVE}.items():
        if " " in phrase and phrase in low:
            hits.append({"term": phrase, "weight": w})

    # Token pass with negation window.
    toks = _tokens(text)
    for i, tok in enumerate(toks):
        w = _POSITIVE.get(tok, _NEGATIVE.get(tok))
        if w is None:
            continue
        window = toks[max(0, i - 2):i]
        if any(neg in window for neg in _NEGATORS):
            w = -w
        hits.append({"term": tok, "weight": w})

    if not hits:
        return {"score": 0.0, "hits": [], "n_terms": 0}

    # Normalised weighted mean -> bounded [-1, 1].
    total = sum(h["weight"] for h in hits)
    score = max(-1.0, min(1.0, total / len(hits)))
    return {"score": round(score, 4), "hits": hits, "n_terms": len(hits)}


def score_headlines(headlines: Sequence[str]) -> Dict[str, Any]:
    """Aggregate tone across many headlines (equal-weighted mean)."""
    scored = [score_text(h) for h in headlines if h]
    if not scored:
        return {"score": 0.0, "n": 0, "per_headline": []}
    agg = sum(s["score"] for s in scored) / len(scored)
    return {"score": round(max(-1.0, min(1.0, agg)), 4),
            "n": len(scored), "per_headline": scored}


def available() -> bool:
    return True


__all__ = ["score_text", "score_headlines", "available"]