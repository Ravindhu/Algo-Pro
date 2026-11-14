"""Economic engine wrapper: macro regime and a risk multiplier, DELEGATED to Blue_engine.

Flow stage: India flow data, Global cues, Economic engine
Status: PHASE 5 IMPLEMENTED (wrap; advice-only multiplier, bounded to [0.5, 1.0])
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/economics/economic_engine.py
(EconomicEngine), src/quant/blue_engine/economics/regime.py, src/quant/blue_engine/economics/scoring.py

WHAT THE OUTPUT IS ALLOWED TO DO
--------------------------------
Blue's engine produces a ``gross_exposure_multiplier`` that can exceed 1.0 (it may
leverage a risk-on book). AGENTS.md bounds the economic agent's influence to
``[0.5, 1.0]``: macro advice may reduce risk, never increase it. ``risk_multiplier``
therefore clamps the Blue number into that band, and every payload says the
multiplier was clamped so a reviewer can see the difference.

Stale data may not size risk up. Blue already caps a stale feed at 1.0; this
wrapper re-checks ``source_meta.stale`` itself and caps again, because a value
that arrived through a JSON round-trip is not evidence that the check ran.

The engine is PURE: macro inputs arrive as a dict, no network, no LLM.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["EconomicUnavailable", "MIN_MULTIPLIER", "MAX_MULTIPLIER",
           "run_economic", "risk_multiplier"]

#: Advice band from AGENTS.md. Lower bound: a macro crisis halves the book, not
#: more - sizing decisions belong to the portfolio stage, not to news.
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 1.0


class EconomicUnavailable(RuntimeError):
    """Blue's economic engine cannot be loaded."""


def _engine_cls():
    try:
        module = load("economics.economic_engine")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise EconomicUnavailable(f"Blue economic engine unavailable: {exc}") from exc
    return module.EconomicEngine


def run_economic(config: Optional[Dict[str, Any]] = None,
                 macro: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 headlines: Optional[List[str]] = None,
                 as_of: Any = None) -> Dict[str, Any]:
    """Run Blue's economic engine on a macro block. Never raises.

    ``macro`` keys are ``{"value": ..., "previous": ..., "unit": ...}`` dicts or
    bare numbers (gdp_growth, inflation, employment, ...). Priority is
    ``macro`` > ``provider()`` > ``config["macro"]``, which is Blue's own order.
    """
    cls = _engine_cls()
    context: Dict[str, Any] = {}
    if macro is not None:
        context["macro"] = dict(macro)
    if headlines:
        context["headlines"] = list(headlines)
    if as_of is not None:
        context["as_of"] = as_of
    engine = cls(dict(config or {}), provider=provider)
    result = engine.run(context)
    return {
        "ok": bool(getattr(result, "ok", False)),
        "engine": getattr(result, "engine", "economic"),
        "data": dict(getattr(result, "data", {}) or {}),
        "warnings": list(getattr(result, "warnings", []) or []),
        "error": getattr(result, "error", None),
    }


def risk_multiplier(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bound a Blue economic payload to the advice band. Never raises.

    Returns ``{multiplier, raw, clamped, capped_for_stale, regime, reasons}``.

    Fail-closed rules:
    - No payload, or a non-finite multiplier: the floor is returned, not 1.0.
      A multiplier we cannot read is an unknown, and an unknown may not spend.
    - A missing regime label is reported as ``UNKNOWN``; it is never guessed.
    - ``source_meta.stale`` true caps at 1.0 (no leverage on stale inputs).
    """
    reasons: List[str] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"multiplier": MIN_MULTIPLIER, "raw": None, "clamped": False,
                "capped_for_stale": False, "regime": "UNKNOWN",
                "reasons": ["no economic payload: floor applied"]}

    raw = data.get("gross_exposure_multiplier")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    if value != value or value in (float("inf"), float("-inf")):
        return {"multiplier": MIN_MULTIPLIER, "raw": raw, "clamped": False,
                "capped_for_stale": False, "regime": data.get("regime") or "UNKNOWN",
                "reasons": ["economic multiplier missing or non-finite: floor applied"]}

    meta = data.get("source_meta") or {}
    stale = bool(meta.get("stale")) if isinstance(meta, dict) else False

    out = value
    if stale and out > MAX_MULTIPLIER:
        out = MAX_MULTIPLIER
        reasons.append("macro data flagged STALE: multiplier capped at 1.0")
    clamped = False
    if out > MAX_MULTIPLIER:
        out = MAX_MULTIPLIER
        clamped = True
        reasons.append(f"multiplication above the advice band ({value:.4f}): capped at 1.0")
    if out < MIN_MULTIPLIER:
        out = MIN_MULTIPLIER
        clamped = True
        reasons.append(f"multiplier below the advice band ({value:.4f}): floored at 0.5")

    return {
        "multiplier": round(out, 4),
        "raw": value,
        "clamped": clamped,
        "capped_for_stale": stale and value > MAX_MULTIPLIER,
        "regime": str(data.get("regime") or "UNKNOWN"),
        "reasons": reasons,
    }
