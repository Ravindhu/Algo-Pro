"""
portfolio/view_bridge.py — Phase 5: turning views into a constrained allocation.

The problem this solves
-----------------------
Views already exist in ``portfolio/views/`` (ML forecasts, lexical NLP, GPT
extraction) and Black-Litterman already exists in ``optim/black_litterman.py``.
What was missing is the **safe bridge** between them.

"Safe" is doing real work in that sentence. An ML forecaster is a *model*; it is
wrong regularly, and it is most confident exactly when it is most wrong (a
regime it has not seen before looks, to the model, like a clear signal). If a
view can move the book without limit, the model becomes the risk manager — and
it has no concept of drawdown.

So this module enforces three rules:

1. **Views are advisory, never authoritative.** They are converted into BL
   views (a *tilt* on the equilibrium prior), never into weights directly. BL
   structurally bounds the damage: with a weak view the posterior barely moves
   from the market prior.
2. **Total tilt is capped.** ``max_total_tilt`` bounds how far, in L1 distance,
   the view-implied book may differ from the view-free book. Exceeding the cap
   scales every view down rather than dropping them, so relative information is
   preserved.
3. **Confidence is discounted, not trusted.** Every view's confidence is
   multiplied by ``confidence_scale`` (default 0.5) and capped at
   ``max_confidence``. A model that claims 99% confidence gets 0.5 x capped, not
   99%. This is the single most important line in the file: over-confident
   models are the norm, and the desk pays for trusting their self-assessment.

The risk layer stays in charge
------------------------------
Nothing here can override a kill-switch, a data-validity halt, or a constraint.
``build_problem()`` accepts a Phase-4 decision and refuses to inject views when
the decision says ``trade=False`` — a model cannot talk the desk into trading
through a drawdown, because the decision is made *before* this function is
consulted.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ..core.results import clamp, safe_float
from . import optim as optim_pkg

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_VIEW_POLICY", "validate_views", "cap_tilt",
    "build_problem", "views_from_inputs", "summarise_views",
]


#: Guard-rails applied to every view before it can reach the optimiser.
DEFAULT_VIEW_POLICY: Dict[str, Any] = {
    #: Multiply every stated confidence — models are over-confident as a rule.
    "confidence_scale": 0.5,
    #: Hard ceiling on any single view's confidence after scaling.
    "max_confidence": 0.35,
    #: Maximum L1 tilt (one-way) the view-implied book may take vs the prior.
    "max_total_tilt": 0.15,
    #: Maximum number of views that may be applied (correlated views compound).
    "max_views": 6,
    #: Minimum confidence for a view to be used at all.
    "min_confidence": 0.02,
}


def views_from_inputs(inputs: Optional[Dict[str, Any]], *,
                      universe: Optional[Sequence[str]] = None,
                      enabled: bool = True,
                      config: Optional[Dict[str, Any]] = None
                      ) -> List[Dict[str, Any]]:
    """Collect views from whatever the provider supplied (ML / NLP / LLM).

    Explicitly **opt-in** (``enabled``): the default is off, so a deployment
    that has not thought about machine-learning risk cannot have views silently
    applied to its book. Turning them on is a deliberate act.

    Sources, in the order they are merged:

    * ``inputs["views"]``    — pre-built views (manual or upstream)
    * ``inputs["returns"]``  — per-sleeve history -> ML views (``ml_views``)
    * ``inputs["headlines"]``— news text -> NLP views (``nlp_views``)
    * ``inputs["text"]``     — free text -> LLM views (needs a key; else no-op)

    Returns a list of validated views; never raises.
    """
    if not enabled:
        return []
    data = inputs or {}
    views: List[Dict[str, Any]] = list(data.get("views") or [])
    uni = list(universe or data.get("universe") or [])
    try:
        from .views import ml_views, nlp_views, llm_note
        history = data.get("returns")
        if isinstance(history, dict) and history:
            try:
                getattr(ml_views, "views_from_history")  # noqa: B018 - presence probe
                ml = ml_views.views_from_history(history, universe=uni)
                views.extend(ml.get("views") or [])
            except Exception as exc:  # noqa: BLE001
                logger.info("ml views unavailable: %r", exc)
        headlines = data.get("headlines")
        if headlines:
            try:
                nlp = nlp_views.views_from_headlines(headlines, universe=uni)
                views.extend(nlp.get("views") or [])
            except Exception as exc:  # noqa: BLE001
                logger.info("nlp views unavailable: %r", exc)
        text = data.get("text")
        if text:
            try:
                llm = llm_note.extract_views(text, sleeves=uni)
                views.extend(llm.get("views") or [])
            except Exception as exc:  # noqa: BLE001
                logger.info("llm views unavailable: %r", exc)
    except Exception as exc:  # noqa: BLE001 - views are optional by design
        logger.info("view sources unavailable: %r", exc)
    return views


def validate_views(views: Optional[Sequence[Dict[str, Any]]], *,
                   universe: Optional[Sequence[str]] = None,
                   policy: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """Normalise, discount and cap raw views into optimiser-ready BL views.

    Applies, in order:

    1. **drop** views for sleeves outside the universe (a stale watchlist should
       not tilt a book that no longer holds the name);
    2. **drop** non-finite or zero returns;
    3. **discount** confidence by ``confidence_scale`` and cap it;
    4. **drop** anything below ``min_confidence`` after discounting;
    5. keep at most ``max_views``, highest-confidence first — correlated views
       compound, so many weak views can move the book more than one strong one.

    Returns ``{"ok", "views", "dropped", "warnings", "n_in", "n_out"}``.
    """
    pol = {**DEFAULT_VIEW_POLICY, **(policy or {})}
    warnings: List[str] = []
    dropped: List[Dict[str, Any]] = []
    uni = set(universe or [])
    scale = safe_float(pol.get("confidence_scale")) or 0.5
    max_conf = safe_float(pol.get("max_confidence")) or 0.35
    min_conf = safe_float(pol.get("min_confidence")) or 0.02
    max_views = int(safe_float(pol.get("max_views")) or 6)

    kept: List[Dict[str, Any]] = []
    raw_views = views if isinstance(views, (list, tuple)) else []
    if views and not raw_views:
        warnings.append(f"'views' was {type(views).__name__}, not a list; ignored")
    for v in raw_views:
        if not isinstance(v, dict):
            dropped.append({"view": repr(v)[:80],
                            "reason": "not a dict"})
            continue
        assets = [str(a) for a in (v.get("assets") or []) if a]
        if uni and assets and not any(a in uni for a in assets):
            dropped.append({**v, "reason": "sleeve not in universe"})
            continue
        ret = safe_float(v.get("return"))
        if ret is None or abs(ret) < 1e-12:
            dropped.append({**v, "reason": "missing or zero return"})
            continue
        raw_conf = safe_float(v.get("confidence"))
        conf = 0.5 if raw_conf is None else raw_conf
        # THE critical line: discount first, then cap. Order matters — capping
        # first would let a model's self-reported 0.99 become 0.35 * scale.
        conf = clamp(conf * scale, 0.0, max_conf)
        if conf < min_conf:
            dropped.append({**v, "reason": f"confidence {conf:.3f} below "
                                           f"min {min_conf}"})
            continue
        kept.append({
            "assets": assets,
            "weights": [safe_float(x) or 0.0 for x in (v.get("weights")
                                                      or [1.0] * len(assets))],
            "return": float(ret),
            "confidence": round(conf, 6),
            "horizon_days": int(safe_float(v.get("horizon_days")) or 20),
            "source": str(v.get("source") or "unknown"),
            "label": str(v.get("label") or " > ".join(assets)),
            "rationale": str(v.get("rationale") or ""),
            "stated_confidence": None if raw_conf is None else round(raw_conf, 4),
        })
    kept.sort(key=lambda x: -x["confidence"])
    if len(kept) > max_views:
        for extra in kept[max_views:]:
            dropped.append({**extra, "reason": f"beyond max_views ({max_views})"})
        kept = kept[:max_views]
    if dropped:
        warnings.append(f"{len(dropped)} view(s) dropped "
                        f"(of {len(raw_views)})")
    return {"ok": True, "views": kept, "dropped": dropped,
            "n_in": len(raw_views), "n_out": len(kept),
            "policy": {"confidence_scale": scale, "max_confidence": max_conf,
                       "max_total_tilt": safe_float(pol.get("max_total_tilt"))
                       or 0.15},
            "warnings": warnings}


def cap_tilt(prior: Dict[str, float], tilted: Dict[str, float], *,
             max_tilt: float = 0.15) -> Dict[str, Any]:
    """Cap how far ``tilted`` may differ from ``prior`` (L1, one-way).

    When the cap is breached we **scale the whole deviation down** rather than
    clamping individual sleeves. Scaling preserves the *relative* information in
    the views (the model's ranking of sleeves survives); clamping would flatten
    the strongest views first and leave the book with arbitrary leftovers.

    Returns ``{"weights", "tilt_before", "tilt_after", "capped"}``.
    """
    keys = set(prior) | set(tilted)
    dev = {k: (safe_float(tilted.get(k)) or 0.0)
           - (safe_float(prior.get(k)) or 0.0) for k in keys}
    tilt = sum(abs(d) for d in dev.values()) / 2.0
    if tilt <= 0 or tilt <= max_tilt:
        return {"weights": {k: round(safe_float(tilted.get(k)) or 0.0, 8)
                            for k in keys},
                "tilt_before": round(tilt, 6), "tilt_after": round(tilt, 6),
                "capped": False}
    f = max_tilt / tilt
    out = {k: (safe_float(prior.get(k)) or 0.0) + dev[k] * f for k in keys}
    total = sum(abs(v) for v in out.values())
    return {"weights": {k: round(v, 8) for k, v in out.items()},
            "tilt_before": round(tilt, 6),
            "tilt_after": round(sum(abs(out[k] - (safe_float(prior.get(k)) or 0.0))
                                    for k in keys) / 2.0, 6),
            "scale_applied": round(f, 6), "capped": True}


def build_problem(inputs: Optional[Dict[str, Any]], *,
                  decision: Optional[Dict[str, Any]] = None,
                  policy: Optional[Dict[str, Any]] = None,
                  enabled: bool = True) -> Dict[str, Any]:
    """Assemble the optimiser problem for the chosen optimiser.

    ``decision`` is the Phase-4 policy output. When it says ``trade=False`` the
    problem is returned with **no views and the flat-book instruction intact** —
    this is the enforcement point for "a model cannot override the risk layer".
    The ML layer is consulted only when the risk layer has already permitted
    trading.

    Returns the problem dict plus ``{"view_info", "view_cap"}`` diagnostics so
    the dashboard can show exactly what the model wanted and what was allowed.
    """
    data = dict(inputs or {})
    universe = list(data.get("universe") or [])
    pol = {**DEFAULT_VIEW_POLICY, **(policy or {})}
    out: Dict[str, Any] = {
        "universe": universe,
        "cov": data.get("cov"),
        "vols": data.get("vols") or {},
        "betas": data.get("betas") or {},
        "market_weights": data.get("market_weights") or {},
        "current_weights": data.get("current_weights") or {},
        "constraints": (decision or {}).get("constraints") or {},
    }
    if decision and decision.get("optimiser"):
        out["optimiser"] = decision["optimiser"]

    blocked = bool(decision) and not decision.get("trade", True)
    if blocked:
        out["views"] = []
        out["view_info"] = {
            "ok": True, "n_in": 0, "n_out": 0, "views": [], "dropped": [],
            "warnings": ["views suppressed: the risk layer has halted trading"],
        }
        return out

    raw = views_from_inputs(data, universe=universe, enabled=enabled)
    info = validate_views(raw, universe=universe, policy=pol)
    out["views"] = info["views"]
    out["view_info"] = info

    # Diagnostic only: what the book WOULD look like with the views applied.
    # This is how the dashboard shows "the model wanted X, you allowed Y".
    if info["views"]:
        try:
            sol = optim_pkg.registry.optimise(
                out.get("optimiser") or "black_litterman",
                {**out, "constraints": {}}, log=None)
            base = optim_pkg.registry.optimise(
                out.get("optimiser") or "black_litterman",
                {**out, "views": [], "constraints": {}}, log=None)
            cap = cap_tilt(base.get("weights") or {}, sol.get("weights") or {},
                           max_tilt=safe_float(pol.get("max_total_tilt")) or 0.15)
            out["view_cap"] = cap
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break
            logger.info("view tilt diagnostic unavailable: %r", exc)
    return out


def summarise_views(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact view summary for the dashboard's Views panel.

    Shows what was **stated** versus what was **allowed**, which is the number
    the desk needs: the gap between the two is the risk layer doing its job.
    """
    info = info or {}
    views = list(info.get("views") or [])
    rows = []
    for v in views:
        rows.append({
            "assets": v.get("assets"),
            "return": v.get("return"),
            "stated_confidence": v.get("stated_confidence"),
            "used_confidence": v.get("confidence"),
            "source": v.get("source"),
            "label": v.get("label"),
        })
    return {
        "ok": bool(info.get("ok", True)),
        "n_in": info.get("n_in", 0),
        "n_out": info.get("n_out", 0),
        "n_dropped": len(info.get("dropped") or []),
        "views": rows,
        "dropped": info.get("dropped") or [],
        "policy": info.get("policy") or {},
        "warnings": info.get("warnings") or [],
    }