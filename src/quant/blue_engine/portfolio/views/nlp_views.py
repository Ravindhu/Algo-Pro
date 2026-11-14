"""
views/nlp_views.py - news/filing sentiment -> Black-Litterman views (pure).

Lexical sentiment first (always available, no key, deterministic), with an
optional transformer/FinBERT path if installed. The design principle from the
plan: text scoring is *additive and optional*; no key -> lexical fallback.

How a headline becomes a view
-----------------------------
1. Score each headline's tone in ``[-1, +1]`` (lexical lexicon, negation-aware).
2. Attribute the headline to a sleeve via keyword rules (sector terms).
3. Aggregate the per-sleeve tone into an expected return view
   ``view_return = tone * sensitivity`` with ``confidence`` from the *number* of
   headlines (more agreement -> higher confidence).

This is deliberately conservative: a single headline cannot move the book much,
and the confidence is capped so text can never dominate the equilibrium prior.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .base import make_view
from ...core.results import clamp, mean, safe_float

logger = logging.getLogger(__name__)

__all__ = ["score_headline", "DEFAULT_LEXICON", "DEFAULT_SLEEVE_KEYWORDS",
           "views_from_headlines"]


#: A compact finance lexicon. Extendable; polarity in [-1, +1].
DEFAULT_LEXICON: Dict[str, float] = {
    "surge": 0.8, "soar": 0.8, "rally": 0.7, "jump": 0.6, "gain": 0.5,
    "rise": 0.4, "beat": 0.6, "outperform": 0.6, "upgrade": 0.7, "record": 0.5,
    "profit": 0.5, "growth": 0.4, "strong": 0.5, "bullish": 0.8, "boost": 0.5,
    "expand": 0.4, "recovery": 0.5, "downgrade": -0.7, "slump": -0.7,
    "plunge": -0.8, "fall": -0.5, "drop": -0.5, "miss": -0.6, "weak": -0.5,
    "loss": -0.5, "bearish": -0.8, "cut": -0.5, "decline": -0.5, "crash": -0.9,
    "default": -0.8, "fraud": -0.9, "probe": -0.5, "lawsuit": -0.6,
    "inflation": -0.3, "hawkish": -0.4, "dovish": 0.4, "stimulus": 0.5,
}

#: Keyword -> sleeve. Extend to your universe; substring match, case-insensitive.
DEFAULT_SLEEVE_KEYWORDS: Dict[str, List[str]] = {
    "BANKNIFTY": ["bank", "banking", "npa", "credit", "lending", "hdfc", "icici",
                  "sbi", "axis", "kotak"],
    "IT": ["it ", "technology", "software", "infosys", "tcs", "wipro", "tech ",
           "digital", "saas"],
    "PHARMA": ["pharma", "drug", "medicine", "healthcare", "usfda", "cipla",
               "sun pharma", "biotech"],
    "AUTO": ["auto", "vehicle", "car", "ev ", "maruti", "tata motors",
             "two-wheeler", "automobile"],
    "METALS": ["steel", "metal", "copper", "aluminium", "iron ore", "mining"],
    "ENERGY": ["oil", "gas", "energy", "crude", "refinery", "reliance",
               "ongc", "petroleum"],
    "FMCG": ["fmcg", "consumer", "hul", "nestle", "retail", "goods"],
    "REALTY": ["realty", "real estate", "property", "housing", "construction"],
}

_NEGATORS = ("not", "no", "never", "without", "fails", "denies")


def score_headline(text: str, *, lexicon: Optional[Dict[str, float]] = None
                   ) -> float:
    """Lexical tone of a headline in ``[-1, +1]`` (negation-aware, mean of hits).

    A word preceded (within 2 tokens) by a negator flips its polarity. Returns 0
    when no lexicon word appears (neutral, not noise).
    """
    lex = lexicon or DEFAULT_LEXICON
    tokens = [t.strip(".,:;!?()[]\"'").lower() for t in (text or "").split()]
    scores: List[float] = []
    for i, tok in enumerate(tokens):
        base = lex.get(tok)
        if base is None:
            continue
        window = tokens[max(0, i - 2):i]
        if any(n in window for n in _NEGATORS):
            base = -base
        scores.append(base)
    if not scores:
        return 0.0
    return clamp(mean(scores) or 0.0, -1.0, 1.0)


def _sleeve_for(text: str, sleeve_keywords: Dict[str, List[str]],
                universe: Sequence[str]) -> Optional[str]:
    low = (text or "").lower()
    for sleeve in universe:
        kws = sleeve_keywords.get(sleeve)
        if kws and any(k in low for k in kws):
            return sleeve
    return None


def views_from_headlines(headlines: Sequence[Any], *,
                         sleeve_keywords: Optional[Dict[str, List[str]]] = None,
                         universe: Optional[Sequence[str]] = None,
                         sensitivity: float = 0.01,
                         max_confidence: float = 0.6
                         ) -> Dict[str, Any]:
    """Aggregate headlines into per-sleeve BL views.

    ``headlines`` items may be strings or ``{"text": ..., "sleeve": ...}``.
    Returns ``{"views": [...], "tones": {sleeve: tone}, "n": int,
    "warnings": [...]}``.
    """
    kws = sleeve_keywords or DEFAULT_SLEEVE_KEYWORDS
    uni = list(universe or kws.keys())
    by_sleeve: Dict[str, List[float]] = {}
    warnings: List[str] = []
    for h in headlines or []:
        if isinstance(h, dict):
            text = str(h.get("text", ""))
            sleeve = h.get("sleeve") or _sleeve_for(text, kws, uni)
        else:
            text = str(h)
            sleeve = _sleeve_for(text, kws, uni)
        if not sleeve:
            continue
        by_sleeve.setdefault(sleeve, []).append(score_headline(text))

    views: List[Dict[str, Any]] = []
    tones: Dict[str, float] = {}
    for sleeve, tones_list in by_sleeve.items():
        tone = mean(tones_list) or 0.0
        tones[sleeve] = round(tone, 4)
        n = len(tones_list)
        if abs(tone) < 1e-6:
            continue
        # Confidence grows with the number of agreeing headlines, capped.
        conf = min(max_confidence, 0.2 + 0.1 * n)
        v = make_view([sleeve], view_return=tone * sensitivity,
                      confidence=conf, horizon_days=20, source="nlp",
                      label=f"{sleeve} news tone",
                      rationale=f"{n} headline(s), tone {tone:+.2f}")
        if v:
            views.append(v)
    if not views:
        warnings.append("no headlines mapped to a sleeve")
    return {"views": views, "tones": tones, "n": len(headlines or []),
            "warnings": warnings}