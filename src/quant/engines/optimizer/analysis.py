"""
optimizer/analysis.py - the three-step visual workflow, as data.

THE ORDER IS THE PRODUCT
------------------------
AGENTS.md prescribes reading the study in a fixed order:
    1. param importances  -> "which 3-4 parameters are driving the ship?"
    2. slice plots        -> each important parameter's individual sweet spot
    3. parallel coordinates -> how they weave together at peak Sharpe
This module computes the DATA for those steps; ``plots.py`` renders them.
Splitting the two means every artefact is testable without plotly, and the
dashboard page can render either the figures or the raw tables.

WHAT "SWEET SPOT" MEANS (decided here, used by the plot)
--------------------------------------------------------
The slice data is binned by parameter value with the bin *median* score. Two
things matter and both are computed:

    * argmax bin  - where the median score peaks
    * plateau     - the WIDEST run of adjacent bins whose median is within
                    PLATEAU_TOLERANCE of the global best median

Reporting only the argmax would be a disservice: a one-bin spike is usually
noise (few trials land there), while a wide plateau is a parameter region you
can actually trade inside without the strategy being knife-edge tuned. The
recommendation logic therefore prefers the plateau centre when a plateau of
>= 2 bins exists.

CATEGORICAL PARAMETERS
----------------------
No bins and no plateau - the "sweet spot" of a category is simply the best-
scoring value(s) with their mean score. The engine's importance plot renders
these as bars, the slice renders them as bars, and they are excluded from the
parallel-coordinate numeric axis (a fake numeric axis would imply an order
that does not exist).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config, logger

__all__ = ["importance_report", "slice_report", "parallel_report",
           "top_k_params", "visual_workflow", "PLATEAU_TOLERANCE"]


#: How close a bin's median must be to the best median to count as plateau
#: (fraction of the best median's absolute value, plus a floor for medians
#: near zero). 0.05 = within 5%. Generous enough to survive bin noise, tight
#: enough that a genuinely different region does not get merged in.
PLATEAU_TOLERANCE = 0.05


# --------------------------------------------------------------------------- #
# Step 1 - importance
# --------------------------------------------------------------------------- #


def importance_report(study: Any) -> Dict[str, Any]:
    """Step 1 data: which parameters are driving the ship.

    Wraps ``Study.param_importance()`` and adds the top-k selection plus the
    under-budget warning the dashboard must show.
    """
    imp = study.param_importance()
    imp["top_k"] = top_k_params(imp)
    return imp


def top_k_params(importance: Dict[str, Any],
                 k: Optional[int] = None) -> List[str]:
    """The k parameters to carry into steps 2 and 3.

    k defaults to ``config.DEFAULT_TOP_K`` (4). Falls back sensibly when the
    importance ranking is empty (too few trials): the parameter list is used in
    space order, because some ordering beats an empty workflow and the
    under-budget flag tells the operator not to over-trust it.
    """
    k = int(k or config.DEFAULT_TOP_K)
    ranking = importance.get("ranking") or []
    if ranking:
        return ranking[:k]
    return []  # caller decides the fallback from space order


# --------------------------------------------------------------------------- #
# Step 2 - slice
# --------------------------------------------------------------------------- #


def slice_report(study: Any, params: Optional[Sequence[str]] = None,
                 *, n_bins: int = 8) -> Dict[str, Any]:
    """Step 2 data: per-parameter sweet-spot curves for the named params.

    Returns ``{"ok", "slices": [ {name, kind, points|categories, argmax,
    plateau, note} ], "n_trials_used"}``.

    The plateau is reported as a [lo, hi] value pair (numeric) so the report can
    say "fast=8, plateau 6-11" rather than nakedly pointing at one value.
    """
    usable = [t for t in study.trials if t.ok and t.value is not None]
    if len(usable) < 8:
        return {"ok": False, "slices": [], "n_trials_used": len(usable),
                "reason": f"only {len(usable)} scoreable trials; need >= 8"}

    if params:
        names = [n for n in params]
    else:
        names = sorted({k for t in usable for k in t.params})

    slices: List[Dict[str, Any]] = []
    for name in names:
        xs = [(t.params.get(name), float(t.value)) for t in usable
              if t.params.get(name) is not None]
        if not xs:
            continue
        is_numeric = all(isinstance(x, (int, float)) and
                         not isinstance(x, bool) for x, _ in xs)
        entry: Dict[str, Any] = {"name": name,
                                 "kind": "numeric" if is_numeric else "categorical"}
        if is_numeric and len({x for x, _ in xs}) > 2:
            bins = _quantile_bins([x for x, _ in xs], n_bins)
            points: List[Dict[str, Any]] = []
            best_bin, plateau = None, None
            for lo, hi in bins:
                ys = [y for x, y in xs if lo <= x <= hi]
                if not ys:
                    continue
                med = _median(ys)
                mid = 0.5 * (lo + hi)
                points.append({"lo": lo, "hi": hi, "mid": mid,
                               "median_score": med, "n": len(ys)})
            if points:
                best = max(points, key=lambda p: p["median_score"])
                best_bin = {"mid": best["mid"],
                            "median_score": best["median_score"],
                            "lo": best["lo"], "hi": best["hi"]}
                plateau = _plateau(points, best)
            entry.update({"points": points, "argmax": best_bin,
                          "plateau": plateau})
            note = (f"sweet spot ~ {best_bin['mid']:.4g} "
                    f"(plateau {plateau[0]:.4g}-{plateau[1]:.4g})"
                    if (best_bin and plateau) else
                    (f"sweet spot ~ {best_bin['mid']:.4g}" if best_bin else
                     "not enough variation"))
            entry["note"] = note
        else:
            by_cat: Dict[Any, List[float]] = {}
            for x, y in xs:
                by_cat.setdefault(x, []).append(y)
            cats = [{"value": v, "mean_score": sum(ys) / len(ys),
                     "median_score": _median(ys), "n": len(ys)}
                    for v, ys in by_cat.items()]
            cats.sort(key=lambda c: c["median_score"], reverse=True)
            best_cat = cats[0] if cats else None
            entry.update({"categories": cats, "argmax": best_cat,
                          "plateau": None,
                          "note": (f"best category: {best_cat['value']} "
                                   f"(median {best_cat['median_score']:.3f})"
                                   if best_cat else "no data")})
        slices.append(entry)
    return {"ok": bool(slices), "slices": slices,
            "n_trials_used": len(usable)}


def _plateau(points: List[Dict[str, Any]],
             best: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Widest adjacent run within PLATEAU_TOLERANCE of the best median."""
    tol = PLATEAU_TOLERANCE * max(1.0, abs(best["median_score"]))
    thresh = best["median_score"] - tol
    idx = points.index(best)
    lo_i = idx
    while lo_i > 0 and points[lo_i - 1]["median_score"] >= thresh:
        lo_i -= 1
    hi_i = idx
    while hi_i < len(points) - 1 and points[hi_i + 1]["median_score"] >= thresh:
        hi_i += 1
    lo, hi = points[lo_i]["lo"], points[hi_i]["hi"]
    if hi <= lo:
        return None
    return (lo, hi)


def _quantile_bins(values: List[float], n_bins: int) -> List[Tuple[float, float]]:
    """Equal-count bin edges over ``values``; returns [(lo, hi), ...]."""
    vs = sorted(float(v) for v in values)
    if len(vs) < n_bins:
        return [(vs[0], vs[-1])]
    edges = [vs[0]] + [vs[int(i * (len(vs) - 1) / n_bins)]
                       for i in range(1, n_bins)] + [vs[-1]]
    out: List[Tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        if b > a:
            out.append((a, b))
    return out


def _median(xs: Sequence[float]) -> float:
    s = sorted(float(x) for x in xs)
    n = len(s)
    if not n:
        return float("nan")
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


# --------------------------------------------------------------------------- #
# Step 3 - parallel coordinates
# --------------------------------------------------------------------------- #


def parallel_report(study: Any, params: Optional[Sequence[str]] = None, *,
                    top_fraction: float = 0.30) -> Dict[str, Any]:
    """Step 3 data: one normalised line per trial over the named params.

    Trials are kept if their score is within the top ``top_fraction`` (the
    "filtering only for those important parameters ... to see how they weave
    together to hit peak profits" step), which is what stops the chart from
    being a hairball of 200 identical-looking lines.

    Each numeric axis is min-max normalised to [0,1] using the FULL trial set's
    range (not just the top trials), so the filtered lines show where the good
    region sits *within* the searched range - the whole point of the chart.
    Categorical axes are placed at integer positions by first-appearance order.
    """
    usable = [t for t in study.trials if t.ok and t.value is not None]
    if len(usable) < 10:
        return {"ok": False, "axes": [], "lines": [],
                "n_trials_used": len(usable),
                "reason": f"only {len(usable)} scoreable trials; need >= 10"}

    if params:
        names = list(params)
    else:
        names = sorted({k for t in usable for k in t.params})

    # Axis ranges from the full set.
    ranges: Dict[str, Dict[str, Any]] = {}
    for name in names:
        vals = [t.params.get(name) for t in usable if t.params.get(name) is not None]
        if not vals:
            continue
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            ranges[name] = {"kind": "numeric",
                            "lo": min(float(v) for v in vals),
                            "hi": max(float(v) for v in vals)}
        else:
            order: List[Any] = []
            for v in vals:
                if v not in order:
                    order.append(v)
            ranges[name] = {"kind": "categorical", "order": order}

    scored = sorted(usable, key=lambda t: float(t.value), reverse=True)
    n_top = max(3, int(round(top_fraction * len(scored))))
    top = scored[:n_top]

    lines: List[Dict[str, Any]] = []
    for t in top:
        coords: Dict[str, Optional[float]] = {}
        for name, r in ranges.items():
            v = t.params.get(name)
            if v is None:
                coords[name] = None
            elif r["kind"] == "numeric":
                lo, hi = r["lo"], r["hi"]
                coords[name] = 0.5 if hi <= lo else \
                    min(1.0, max(0.0, (float(v) - lo) / (hi - lo)))
            else:
                order = r["order"]
                coords[name] = order.index(v) / max(1, len(order) - 1)
        lines.append({"number": t.number, "score": t.value,
                      "coords": coords, "params": dict(t.params)})

    return {"ok": bool(lines), "axes": [{"name": n, **ranges[n]}
                                        for n in names if n in ranges],
            "lines": lines, "n_trials_used": len(usable),
            "top_fraction": top_fraction,
            "score_range": {"best": float(top[0].value),
                            "cutoff": float(top[-1].value)}}


# --------------------------------------------------------------------------- #
# The whole workflow
# --------------------------------------------------------------------------- #


def visual_workflow(study: Any, *, k: Optional[int] = None) -> Dict[str, Any]:
    """Steps 1 -> 2 -> 3 in the AGENTS.md order, in one call.

    This is what the CLI's ``--visual-workflow`` and the dashboard page call.
    Returns all three artefacts plus the recommended parameter set, so a report
    is one object that can be JSON-serialised, diffed, and re-rendered later
    without re-running the study.

    Returned fields (always present, so a consumer never has to guard):
        ``ok``             - the workflow was computable. Tells you "the
                             engine produced SOME ranking", NOT "you can
                             trust the ranking". See ``provisional``.
        ``provisional``    - True when the ranking is below the
                             ``config.MIN_TRIALS_FOR_IMPORTANCE`` bar, i.e.
                             fANOVA-equivalent on fewer than 50 trials is
                             not statistically stable. A dashboard should
                             render an amber "UNDER-BUDGET" banner when this
                             is True. This is AUDIT FINDING F5: previously
                             the only signal of "this ranking is thin" was
                             buried inside ``step1_importance.under_budget``,
                             and consumers that only checked ``ok`` would
                             not realise the ranking was provisional.
        ``under_budget``   - mirror of ``step1_importance.under_budget`` for
                             convenience. Same value as ``provisional``.
        ``step1_importance`` - the importance-report artefact.
        ``step2_slice``    - the slice-report artefact.
        ``step3_parallel`` - the parallel-coordinates-report artefact.
        ``important_params`` - the top-k names the slice/parallel steps used.
        ``fallback_note``  - non-None iff the params were chosen in space
                             order rather than from the importance ranking.
        ``n_trials_used``  - scoreable trial count behind the ranking.
    """
    imp = importance_report(study)
    ranking = imp.get("ranking") or []
    if ranking:
        chosen = ranking[:int(k or config.DEFAULT_TOP_K)]
        fallback_note = None
    else:
        # Under-budget: pick from space order so the workflow still produces
        # something, and SAY that is what happened.
        names = sorted({k2 for t in study.trials for k2 in (t.params or {})})
        chosen = names[:int(k or config.DEFAULT_TOP_K)]
        fallback_note = "importance under-budget; params chosen in space order"

    sl = slice_report(study, chosen)
    pc = parallel_report(study, chosen)

    # AUDIT FINDING F5 (2026-10-01). Surface "is this ranking trustworthy?"
    # as a first-class field. ``ok`` is "the engine computed something";
    # ``provisional`` is "do not act on the ranking alone". A consumer that
    # only checks ``ok`` is the bug the audit closed.
    under_budget = bool(imp.get("under_budget", True))
    return {
        "ok": imp.get("ok", False) or bool(ranking),
        "provisional": under_budget,
        "under_budget": under_budget,
        "step1_importance": imp,
        "step2_slice": sl,
        "step3_parallel": pc,
        "important_params": chosen,
        "fallback_note": fallback_note,
        "n_trials_used": imp.get("n_trials_used", 0),
    }
