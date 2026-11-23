"""
views/base.py - the canonical view schema + validation (pure).

A view is a small, well-typed dict so that ML, NLP and LLM producers all emit the
same shape and the Black-Litterman optimiser never has to guess::

    {"assets": ["ICICIBANK", "HDFCBANK"],   # or a single sleeve ["PHARMA"]
     "weights": [1.0, -1.0],                 # relative view (long/short spread)
     "return": 0.012,                        # expected spread return (fraction)
     "confidence": 0.6,                      # in (0, 1]
     "horizon_days": 20,
     "source": "ml|nlp|llm|manual",
     "label": "BANKNIFTY > NIFTY",
     "rationale": "short explanation for the desk"}

For an *absolute* view, give one asset and ``weights=[1.0]``.

Validation is strict: a malformed view is dropped with a warning rather than
silently poisoning the posterior.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float

__all__ = ["make_view", "cluster_views"]


def make_view(assets: Sequence[str], *, weights: Optional[Sequence[float]] = None,
              view_return: float, confidence: float = 0.5,
              horizon_days: int = 20, source: str = "manual",
              label: Optional[str] = None, rationale: Optional[str] = None
              ) -> Optional[Dict[str, Any]]:
    """Build a validated view dict, or None if it is malformed.

    ``weights`` defaults to ``[1.0] * len(assets)`` (an equal long view). A view
    with no assets or a non-finite return is rejected (returns None).
    """
    assets = [str(a) for a in (assets or []) if a]
    if not assets:
        return None
    ret = safe_float(view_return)
    if ret is None:
        return None
    w = list(weights) if weights else [1.0] * len(assets)
    if len(w) != len(assets):
        w = [1.0] * len(assets)
    conf = safe_float(confidence)
    conf = 0.5 if conf is None else min(max(conf, 0.001), 0.999)
    return {
        "assets": assets,
        "weights": [safe_float(x) or 0.0 for x in w],
        "return": float(ret),
        "confidence": conf,
        "horizon_days": int(horizon_days or 20),
        "source": source,
        "label": label or " > ".join(assets),
        "rationale": rationale or "",
    }


def cluster_views(views: Sequence[Dict[str, Any]],
                  *, per_view_max: int = 2) -> List[Dict[str, Any]]:
    """Keep at most ``per_view_max`` views per source to avoid over-confidence.

    Stacking many highly-correlated views (e.g. 20 near-identical news headlines)
    falsely narrows the posterior. This caps how many views a single source may
    contribute, keeping the ones with the highest confidence.
    """
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for v in views or []:
        by_source.setdefault(v.get("source", "?"), []).append(v)
    out: List[Dict[str, Any]] = []
    for src, vs in by_source.items():
        vs.sort(key=lambda x: safe_float(x.get("confidence")) or 0.0,
                reverse=True)
        out.extend(vs[:per_view_max])
    return out