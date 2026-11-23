"""
features/surprise.py - value vs expectation (the single biggest accuracy fix).

Markets price the **surprise**, not the level. A CPI print of 5.1% is risk-ON
when consensus was 5.6% and risk-OFF when consensus was 4.6%. The baseline
engine reads only the level (weakness #1 in the plan); this module turns a
reading into a signed, standardised surprise.

Two forecast sources, in priority order
---------------------------------------
1. **External consensus** - if the macro block carries ``forecast`` (e.g. the
   street's CPI estimate), the surprise is simply ``actual - forecast``.
2. **Internal AR(1)**   - otherwise we forecast the next value from the series'
   own history (``x_t ~ a + b * x_{t-1}``) and take the residual. This is a
   dependency-free, robust default when no consensus feed is wired.

The raw surprise is then standardised by its rolling standard deviation so it
is comparable across signals with wildly different units:

    z = (actual - forecast) / rolling_sd(historical surprises)

``z`` is clipped and passed through ``tanh`` by the scorer, so one extreme print
cannot dominate the composite (same saturating philosophy as the baseline).

Purity
------
Pure functions only. No network, no numpy required. Accepts plain lists / dicts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Reuse the dependency-free helpers already in core.
from ...core.results import safe_float, stdev


def _extract_latest(raw: Any) -> Optional[float]:
    if isinstance(raw, dict):
        return safe_float(raw.get("value", raw.get("latest")))
    return safe_float(raw)


def _extract_forecast(raw: Any) -> Optional[float]:
    if isinstance(raw, dict):
        return safe_float(raw.get("forecast", raw.get("consensus",
                                                      raw.get("expected"))))
    return None


def _extract_previous(raw: Any) -> Optional[float]:
    """The prior reading - the last-resort expectation for a change surprise."""
    if isinstance(raw, dict):
        return safe_float(raw.get("previous", raw.get("prior")))
    return None


def _extract_history(raw: Any) -> List[float]:
    """Pull a numeric history list from ``{"history": [...]}`` or similar."""
    if not isinstance(raw, dict):
        return []
    hist = raw.get("history") or raw.get("series") or []
    out: List[float] = []
    for item in hist:
        v = _extract_latest(item)
        if v is not None:
            out.append(v)
    return out


def ar1_forecast(series: Sequence[float]) -> Optional[float]:
    """One-step-ahead AR(1) forecast using OLS on (x_{t-1}, x_t) pairs.

    Returns the predicted next value, or None when there is insufficient data
    (fewer than 3 points) or a degenerate (zero-variance) regressor. Pure and
    dependency-free.
    """
    clean = [float(v) for v in series if v is not None]
    n = len(clean)
    if n < 3:
        return None
    xs = clean[:-1]
    ys = clean[1:]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    var_x = sum((x - mx) ** 2 for x in xs)
    if var_x == 0:
        # Flat series -> best forecast is the level itself.
        return clean[-1]
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
    beta = cov / var_x
    alpha = my - beta * mx
    return alpha + beta * clean[-1]


def rolling_sd(series: Sequence[float], *, min_obs: int = 3) -> Optional[float]:
    """Standard deviation of a series, None when under ``min_obs``."""
    clean = [float(v) for v in series if v is not None]
    if len(clean) < max(2, min_obs):
        return None
    return stdev(clean)


def surprise(raw: Any, *, forecast: Optional[float] = None,
             scale: Optional[float] = None,
             default_scale: float = 1.0) -> Dict[str, Any]:
    """Compute the standardised surprise for one signal.

    Parameters
    ----------
    raw : The macro value block (dict) or a bare number. May carry
          ``forecast``/``consensus`` and ``history``.
    forecast : Explicit forecast override (highest priority).
    scale : Explicit standard-deviation denominator (else derived from the
            history of surprises, else ``default_scale``).

    Returns a dict::

        {"actual": float, "forecast": float|None, "surprise": float|None,
         "z": float|None, "method": "consensus"|"ar1"|"change"|"none"}

    Forecast priority (first available wins), so a surprise is produced whenever
    *any* expectation can be formed:

        1. explicit ``forecast`` argument
        2. ``forecast``/``consensus``/``expected`` in the block   -> "consensus"
        3. AR(1) fit over the block's ``history`` (>= 3 points)   -> "ar1"
        4. the block's own ``previous`` value (change surprise)   -> "change"

    The last fallback is what makes the view *always* populate for a signal
    that carries only ``value`` + ``previous`` (the common live case).
    """
    actual = _extract_latest(raw)
    if actual is None:
        return {"actual": None, "forecast": None, "surprise": None,
                "z": None, "method": "none"}

    fc = safe_float(forecast)
    method = "consensus"
    if fc is None:
        fc = _extract_forecast(raw)
    if fc is None:
        series = _extract_history(raw)
        # Include the current reading so the AR(1) sees the whole trajectory.
        series = series + [actual] if series else []
        fc = ar1_forecast(series) if len(series) >= 3 else None
        method = "ar1" if fc is not None else "none"
    if fc is None:
        # Last resort: the previous reading is the expectation (change surprise).
        # This guarantees the surprise view populates whenever we have a
        # previous value, which is the everyday live-data case.
        fc = _extract_previous(raw)
        if fc is not None:
            method = "change"

    if fc is None:
        return {"actual": actual, "forecast": None, "surprise": None,
                "z": None, "method": "none"}

    s = actual - fc

    # Denominator: prefer an explicit scale, else the dispersion of the
    # history's own surprises (approximated by the history's stdev), else the
    # configured default.
    denom = safe_float(scale)
    if denom is None:
        hist = _extract_history(raw)
        denom = rolling_sd(hist) if hist else None
    if denom is None or denom == 0:
        denom = safe_float(default_scale) or 1.0

    return {"actual": actual, "forecast": fc, "surprise": round(s, 6),
            "z": round(s / denom, 6), "method": method}


def surprise_scores(macro: Dict[str, Any],
                    *, scale_map: Optional[Dict[str, float]] = None
                    ) -> Dict[str, Dict[str, Any]]:
    """Compute surprises for every numeric signal in a macro block.

    ``scale_map`` may supply a per-signal standard-deviation denominator (e.g.
    from a fitted model). A signal now yields a surprise whenever it carries a
    ``forecast`` (method ``consensus``), a 3+ point ``history`` (``ar1``), or
    even just a ``previous`` reading (``change``). Only a block with a bare
    value and no prior at all gets ``method='none'`` - the engine then falls
    back to level-only scoring for that signal.
    """
    scale_map = scale_map or {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, raw in (macro or {}).items():
        if key == "meta":
            continue
        if isinstance(raw, (int, float)) or isinstance(raw, dict):
            out[key] = surprise(raw, scale=scale_map.get(key))
    return out


__all__ = ["surprise", "surprise_scores", "ar1_forecast", "rolling_sd"]