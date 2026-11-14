"""Tail risk: VaR, CVaR, drawdown and named India stress scenarios. Wraps the engine.

Flow stage: Stress and CVaR, Live intraday VaR/CVaR (portfolio risk lane)
Status: PHASE 5 IMPLEMENTED (wrap; measurement only, cannot approve or size an order)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/portfolio/risk/tail.py  (historical_var,
        parametric_var, cornish_fisher_var, cvar, drawdown_series, max_drawdown,
        drawdown_at_risk, portfolio_var, stress_test, DEFAULT_STRESS)

WHY THIS WRAPPER EXISTS
-----------------------
`portfolio/portfolio_risk.py` has ``stress_pnl``, a first-order linear shock on
current weights. That is the right tool for "what if NIFTY gaps -5% today". It is
NOT a tail model: it needs a shock value handed to it and it says nothing about
the shape of the loss distribution.

The engine's ``tail.py`` supplies what was missing:
- VaR and CVaR at 95/99%, historical, parametric and Cornish-Fisher (fat tails);
- drawdown-at-risk on a P&L path;
- five named India scenarios with calibrated shocks (GFC, taper, demonetisation,
  COVID, a VIX spike).

Rule 6 of AGENTS.md puts risk first. Before this module, the v5 flow's "Stress and
CVaR" and "Live intraday VaR" blocks had no implementation on this side at all.

WHAT THIS WRAPPER ADDS
----------------------
1. **Fail-closed breach testing** (`check_limits`). The gate needs a boolean it
   can trust, not a float it has to interpret. A CVaR that cannot be computed is
   reported as unmeasurable, never as safe.
2. **A stable panel shape** for the dashboard, so the same numbers appear on the
   page and in the audit log.
3. **Unit discipline.** Returns are fractions. A caller passing percentages would
   get a 100x understated CVaR, so `_as_fractions` detects and refuses that.

WHAT IT DOES NOT DO
-------------------
- It cannot approve, size, or block an order directly. It returns numbers and a
  breach flag; `risk/gate.py` remains the only path to an order.
- It does not fetch data. You pass a P&L or returns series.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "TailRiskUnavailable",
    "DEFAULT_STRESS",
    "summarise",
    "stress",
    "check_limits",
    "panel",
]


class TailRiskUnavailable(RuntimeError):
    """The engine's tail-risk module cannot be loaded."""


def _module():
    try:
        return load("portfolio.risk.tail")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise TailRiskUnavailable(f"engine tail risk unavailable: {exc}") from exc


#: Named India stress scenarios, read once. Empty when the engine is missing,
#: so an import-time failure degrades the panel instead of killing the process.
def _default_stress() -> Dict[str, Dict[str, Any]]:
    try:
        return dict(_module().DEFAULT_STRESS)
    except TailRiskUnavailable as exc:
        logger.warning("tail risk scenarios unavailable: %s", exc)
        return {}


DEFAULT_STRESS = _default_stress()


def _as_fractions(returns: Sequence[float], *, what: str = "returns") -> List[float]:
    """Refuse a percentage-scale series. Returns must be fractions (0.01 = 1%).

    A series whose median absolute value exceeds 1.0 is almost certainly in
    percent. Computing a CVaR on it silently understates risk by 100x, which is
    the worst possible direction for a risk number to be wrong.
    """
    vals = [float(x) for x in (returns or []) if x is not None]
    if not vals:
        return []
    magnitudes = sorted(abs(v) for v in vals)
    median = magnitudes[len(magnitudes) // 2]
    if median > 1.0:
        raise ValueError(
            f"{what} look like percentages (median |value|={median:.2f}). "
            "Pass fractions: 0.01 means 1%.")
    return vals


def equity_curve(returns: Sequence[float], *, start: float = 1.0) -> List[float]:
    """Compound a returns series into an equity curve, starting at ``start``.

    The engine's ``max_drawdown`` takes an EQUITY series, not returns. Passing
    returns straight in silently treats every periodic return as a price level
    and produces a meaningless drawdown, so the conversion lives here.
    """
    curve = [float(start)]
    for r in returns:
        curve.append(curve[-1] * (1.0 + float(r)))
    return curve


def summarise(returns: Sequence[float], *, levels: Sequence[float] = (0.95, 0.99)
              ) -> Dict[str, Any]:
    """VaR/CVaR/drawdown bundle for one returns series (fractions).

    Returns the engine's ``summarise`` output plus the drawdown computed from a
    compounded equity curve and the levels used. An empty series gives
    ``{"n": 0, ...}`` with NO metric keys, never zeros: a zero CVaR would read
    as "no tail risk", which is a lie about missing data.
    """
    try:
        mod = _module()
    except TailRiskUnavailable as exc:
        return {"n": 0, "error": str(exc), "levels": list(levels)}
    vals = _as_fractions(returns)
    if not vals:
        return {"n": 0, "levels": list(levels),
                "note": "no returns supplied; tail metrics not computed"}
    out = mod.summarise(vals, levels=levels)
    out["max_drawdown"] = mod.max_drawdown(equity_curve(vals))
    out["levels"] = list(levels)
    return out


def stress(beta: Optional[float], *, net_exposure: float = 1.0,
           scenarios: Optional[Dict[str, Dict[str, Any]]] = None
           ) -> List[Dict[str, Any]]:
    """Replay the named India scenarios on the current book.

    ``beta`` None is treated as 1.0 by the engine (market-neutral assumption is
    the honest default when beta is unknown). The result is sorted worst-first.
    """
    try:
        mod = _module()
    except TailRiskUnavailable as exc:
        logger.warning("stress scenarios unavailable: %s", exc)
        return []
    return list(mod.stress_test(beta, net_exposure=net_exposure, scenarios=scenarios))


def check_limits(
    returns: Sequence[float],
    *,
    max_cvar: Optional[float] = None,
    max_drawdown: Optional[float] = None,
    level: float = 0.99,
) -> Dict[str, Any]:
    """Compare tail metrics against limits. Fail closed on anything unmeasurable.

    ``max_cvar`` and ``max_drawdown`` are positive fractions (0.05 = 5%).
    A limit of ``None`` means unset, and an unset limit cannot be passed: the
    result is ``breached=True`` with reason ``limit_not_set``, matching the risk
    gate's convention that an unset limit fails closed for entries.

    Returns::

        {"breached": bool, "reasons": [...], "cvar": float|None,
         "max_drawdown": float|None, "measurable": bool}
    """
    reasons: List[str] = []
    try:
        mod = _module()
    except TailRiskUnavailable as exc:
        return {"breached": True, "reasons": [f"tail risk unavailable: {exc}"],
                "cvar": None, "max_drawdown": None, "measurable": False}

    vals = _as_fractions(returns)
    if not vals:
        return {"breached": True, "reasons": ["no returns: tail risk unmeasurable"],
                "cvar": None, "max_drawdown": None, "measurable": False}

    cvar_val = mod.cvar(vals, level=level)
    dd_val = mod.max_drawdown(equity_curve(vals))

    if max_cvar is None:
        reasons.append("max_cvar limit not set (fails closed)")
    elif cvar_val is None:
        reasons.append("cvar unmeasurable (fails closed)")
    else:
        # CVaR is a loss, so compare magnitudes.
        loss = abs(float(cvar_val))
        if loss > float(max_cvar):
            reasons.append(f"cvar {loss:.4f} exceeds limit {float(max_cvar):.4f}")

    if max_drawdown is None:
        reasons.append("max_drawdown limit not set (fails closed)")
    elif dd_val is None:
        reasons.append("max drawdown unmeasurable (fails closed)")
    else:
        dd = abs(float(dd_val))
        if dd > float(max_drawdown):
            reasons.append(f"max drawdown {dd:.4f} exceeds limit {float(max_drawdown):.4f}")

    return {
        "breached": bool(reasons),
        "reasons": reasons,
        "cvar": None if cvar_val is None else round(float(cvar_val), 6),
        "max_drawdown": None if dd_val is None else round(float(dd_val), 6),
        "measurable": cvar_val is not None and dd_val is not None,
    }


def panel(returns: Sequence[float], *, beta: Optional[float] = None,
          net_exposure: float = 1.0) -> Dict[str, Any]:
    """One dict for the dashboard: tail summary plus stress scenarios.

    Read-only and cheap, so the page can call it on every refresh.
    """
    summary = summarise(returns)
    return {
        "summary": summary,
        "stress": stress(beta, net_exposure=net_exposure),
        "scenarios_available": len(DEFAULT_STRESS),
    }
