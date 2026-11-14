"""Point-in-time feature joins: backward-only as-of alignment across sources.

Flow stage: Feature store, Market data live, Universe and actions
Status: PHASE 5 IMPLEMENTED (wrap; pure functions, no I/O and no trade path)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/ml/features/point_in_time.py  (parse_ts, asof_join,
        asof_value, align_panel)

WHY THIS IS SEPARATE FROM ``data/feature_store.py``
---------------------------------------------------
``data/feature_store.py`` stores ONE series per (feature, symbol) and answers
"what was this value at time t". That is a lookup, and it is correct for that.

It cannot answer the question the pipeline actually asks: *"attach last month's
CPI to every price bar, but never use a release that was published after the
bar's close, and treat a value older than 45 days as unknown."* That is an as-of
join with a staleness bound, and it is the single behaviour that separates a real
model from a beautiful fiction.

This module wraps the engine's implementation rather than reimplementing it,
because getting the tie-breaking and the staleness fence subtly wrong produces a
backtest that looks excellent and loses money.

WHAT THIS WRAPPER ADDS
----------------------
1. **A staleness default.** The engine leaves ``max_staleness_days`` off by
   default, which lets a six-month-old macro print masquerade as current. Here
   the default is bounded, and a caller must pass ``None`` explicitly to disable
   the fence.
2. **A coverage report** (`join_report`) so the pipeline can show how many base
   rows got a real value rather than a null. Silent full-null joins are a common
   and invisible failure.
3. **A refusal of fabricated defaults.** Passing ``default=0`` makes every
   missing observation look like a real zero. The engine documents the hazard;
   this wrapper enforces it.

WHAT IT DOES NOT DO
-------------------
- No I/O, no caching, no writing. Pure data transformation.
- Never touches the trade path.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "PointInTimeUnavailable",
    "DEFAULT_MAX_STALENESS_DAYS",
    "parse_ts",
    "asof_join",
    "asof_value",
    "align_panel",
    "join_report",
]

#: A lookup value older than this is treated as unknown. Two months covers the
#: slowest monthly macro series without letting a stale figure drift forever.
DEFAULT_MAX_STALENESS_DAYS = 60


class PointInTimeUnavailable(RuntimeError):
    """The engine's point-in-time module cannot be loaded."""


def _module():
    try:
        return load("ml.features.point_in_time")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise PointInTimeUnavailable(f"engine point-in-time module unavailable: {exc}") from exc


def parse_ts(value: Any):
    """Parse a timestamp to an aware datetime, or None. Never raises."""
    try:
        return _module().parse_ts(value)
    except PointInTimeUnavailable:
        return None


def asof_join(
    base_rows: Sequence[Dict[str, Any]],
    lookup_rows: Sequence[Dict[str, Any]],
    *,
    base_ts: str = "as_of_ts",
    lookup_ts: str = "as_of_ts",
    columns: Optional[Sequence[str]] = None,
    prefix: str = "",
    default: Any = None,
    max_staleness_days: Optional[int] = DEFAULT_MAX_STALENESS_DAYS,
) -> List[Dict[str, Any]]:
    """Attach the most recent KNOWN lookup values to each base row.

    Backward-only: for a base row at time t, only lookup rows at or before t are
    candidates. Ties resolve to the last such row, so a corrected re-release wins.

    ``max_staleness_days`` defaults to a real bound here (60 days), unlike the
    engine's None. Pass ``None`` explicitly to carry a value forward forever, and
    know that you are then pretending an old print is current.

    ``default`` must not be 0: a fabricated zero is indistinguishable from a real
    observation and will pass every quality gate. Use None and let the null show.
    """
    if isinstance(default, (int, float)) and not isinstance(default, bool) and default == 0:
        raise ValueError(
            "default=0 fabricates an observation. Use None so the null is visible, "
            "or join a source that actually covers these rows.")

    try:
        mod = _module()
    except PointInTimeUnavailable as exc:
        logger.warning("point-in-time join unavailable, returning base rows unchanged: %s", exc)
        return [dict(r) for r in base_rows or []]

    return list(mod.asof_join(
        base_rows, lookup_rows,
        base_ts=base_ts, lookup_ts=lookup_ts, columns=columns,
        prefix=prefix, default=default,
        max_staleness_days=max_staleness_days,
    ))


def asof_value(lookup_rows: Sequence[Dict[str, Any]], at: Any, *,
               lookup_ts: str = "as_of_ts", column: str = "value",
               max_staleness_days: Optional[int] = DEFAULT_MAX_STALENESS_DAYS) -> Any:
    """The single lookup value known at ``at``. None when nothing is known yet."""
    try:
        mod = _module()
    except PointInTimeUnavailable:
        return None
    return mod.asof_value(lookup_rows, at, lookup_ts=lookup_ts, column=column,
                          max_staleness_days=max_staleness_days)


def align_panel(series: Dict[str, Sequence[Dict[str, Any]]], **kwargs: Any) -> List[Dict[str, Any]]:
    """Align several timestamped series onto one row-per-timestamp panel."""
    try:
        mod = _module()
    except PointInTimeUnavailable as exc:
        logger.warning("panel alignment unavailable: %s", exc)
        return []
    return list(mod.align_panel(series, **kwargs))


def join_report(joined: Sequence[Dict[str, Any]], columns: Sequence[str]) -> Dict[str, Any]:
    """Coverage of a join: how many rows got a real value for each column.

    A join that fills 0% of rows is invisible in a feature matrix but fatal to a
    model. This makes it visible:

        {"rows": int, "columns": {name: {"filled": int, "null": int,
                                          "coverage": float}},
         "worst": str|None, "all_empty": bool}
    """
    rows = list(joined or [])
    n = len(rows)
    out: Dict[str, Any] = {"rows": n, "columns": {}, "worst": None, "all_empty": n > 0}
    if n == 0:
        return out

    worst_name, worst_cov = None, 2.0
    for col in columns or []:
        filled = sum(1 for r in rows if r.get(col) is not None)
        cov = filled / n
        out["columns"][col] = {"filled": filled, "null": n - filled,
                               "coverage": round(cov, 4)}
        if cov < worst_cov:
            worst_name, worst_cov = col, cov
        if cov > 0:
            out["all_empty"] = False

    out["worst"] = worst_name
    return out
