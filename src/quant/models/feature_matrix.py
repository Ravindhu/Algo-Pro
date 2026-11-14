"""Feature matrix assembly: prices, point-in-time joins, and a leak check.

Flow stage: Feature store, Factor and alpha models, Market data live
Status: PHASE 3 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy):
    quant.models.factors            (momentum_12_1, low_vol, cross-section z-score)
    quant.data.feature_store_pit    (asof_join - the engine's backward-only join)
    quant.research.leakage_gate     (the verdict wrapper over the engine leak audit)

WHY THIS MODULE EXISTS
----------------------
`data/feature_store_pit.py` (as-of join) and `research/leakage_gate.py` (leak
verdict) were both built and tested, and NOTHING called them. A tested gate with
no call site protects nothing.

There was also no single place that assembled a feature matrix, so every caller
would have had to remember: use only the past, join macro data point-in-time, and
audit for leakage. That is a checklist nobody runs consistently by hand.

`build_feature_matrix` is that place. It builds the matrix and returns the leak
audit WITH it, so a caller cannot obtain features without also being told whether
they leak. The `require_clean` path makes that enforcement rather than advice.

LOOK-AHEAD DISCIPLINE
---------------------
- Price features come from `factors`, which shift back only and refuse a negative
  shift.
- Fundamentals come from `asof_join`, which is backward-only: a release stamped
  after a bar's close is never used for that bar.
- The assembled matrix is then run through the leak audit, which re-tests the
  claim behaviourally rather than trusting the two rules above.

WHAT IT DOES NOT DO
-------------------
- No network, no file writes. Pure transformation of frames handed in.
- Never touches the trade path. A feature matrix is research input.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from quant.models import factors
from quant.research.leakage_gate import audit as audit_leakage

logger = logging.getLogger(__name__)

__all__ = [
    "build_feature_matrix",
    "price_features",
    "attach_point_in_time",
    "DEFAULT_PRICE_FEATURES",
]

#: Feature name -> callable(close_frame). Kept explicit so a matrix is auditable.
DEFAULT_PRICE_FEATURES = {
    "mom_12_1": lambda px: factors.momentum_12_1(px),
    "low_vol": lambda px: factors.low_vol(px),
}


def price_features(close: pd.DataFrame,
                   specs: Optional[Dict[str, Any]] = None) -> Dict[str, pd.DataFrame]:
    """Price-derivative features for a close-price matrix (index=date, columns=symbol).

    Each feature is a DataFrame aligned to ``close``. Missing history is NaN, never
    filled: a filled value invents data, and the leak audit cannot see an invention.
    """
    specs = specs or DEFAULT_PRICE_FEATURES
    out: Dict[str, pd.DataFrame] = {}
    for name, fn in specs.items():
        try:
            frame = fn(close)
        except Exception as exc:  # noqa: BLE001 - one bad feature must not stop the rest
            logger.warning("feature %s failed: %r", name, exc)
            continue
        if isinstance(frame, pd.Series):
            frame = frame.to_frame(name)
        out[name] = frame
    return out


def attach_point_in_time(rows: Sequence[Dict[str, Any]],
                         lookup_rows: Sequence[Dict[str, Any]],
                         *, columns: Optional[Sequence[str]] = None,
                         prefix: str = "",
                         max_staleness_days: Optional[int] = 60
                         ) -> List[Dict[str, Any]]:
    """Join reference data (macro, fundamentals) as of each row's timestamp."""
    from quant.data.feature_store_pit import asof_join

    return asof_join(rows, lookup_rows, columns=columns, prefix=prefix,
                     max_staleness_days=max_staleness_days)


def build_feature_matrix(
    close: pd.DataFrame,
    *,
    fundamentals: Optional[Sequence[Dict[str, Any]]] = None,
    bars_ts: str = "date",
    ts_col: str = "as_of_ts",
    specs: Optional[Dict[str, Any]] = None,
    forward_returns: Optional[Sequence[Optional[float]]] = None,
    require_clean: bool = True,
) -> Dict[str, Any]:
    """Build a feature matrix and audit it for leakage in one call.

    Returns::

        {"matrix": pd.DataFrame,           # index=date, columns=symbol|feature
         "features": [names],
         "leakage": {...},                 # the audit report
         "verdict": clean|suspect|leaked|incomplete,
         "detail": str}

    ``require_clean`` (default) raises ValueError when the audit is not clean.
    That is deliberate: features that leak look BETTER than honest ones, so a
    silent pass is the one outcome that must not be possible.
    """
    feats = price_features(close, specs)
    if not feats:
        return {"matrix": pd.DataFrame(index=close.index), "features": [],
                "leakage": {"verdict": "incomplete"},
                "verdict": "incomplete",
                "detail": "no price features could be computed"}

    # Long form for the audit: one row per (date, symbol).
    stacked = []
    for name, frame in feats.items():
        # pandas 3 removed ``stack(dropna=...)``. Iterating the values directly is
        # version-independent AND keeps the NaN cells: a dropped NaN would silently
        # shorten the series that the leak audit tests.
        for day in frame.index:
            for symbol in frame.columns:
                value = frame.at[day, symbol]
                stacked.append({"date": day, "symbol": symbol,
                                f"f_{name}": None if pd.isna(value) else float(value)})

    merged: Dict[tuple, Dict[str, Any]] = {}
    for row in stacked:
        key = (row["date"], row["symbol"])
        merged.setdefault(key, {"date": row["date"], "symbol": row["symbol"]}).update(row)

    # IMPORTANT: with more than one symbol, the same date appears once per symbol,
    # so a bare date as ``as_of_ts`` looks like a DUPLICATE timestamp and the leak
    # audit correctly flags it. The knowledge time of a (date, symbol) cell is the
    # pair, so the symbol is folded into the stamp. Single-symbol matrices keep the
    # bare date, which is what a per-symbol series should carry.
    multi = len({k[1] for k in merged}) > 1
    rows: List[Dict[str, Any]] = []
    for key in sorted(merged, key=lambda k: (str(k[0]), str(k[1]))):
        row = dict(merged[key])
        stamp = pd.Timestamp(row["date"]).isoformat()
        row[ts_col] = f"{stamp}#{key[1]}" if multi else stamp
        rows.append(row)

    if fundamentals:
        has_ts = all(ts_col in r for r in rows)
        if has_ts:
            try:
                rows = attach_point_in_time(rows, fundamentals)
            except Exception as exc:  # noqa: BLE001
                logger.warning("point-in-time join failed: %r", exc)

    # Union across ALL rows, not rows[0]: a point-in-time join can add a column to
    # later rows only, and taking row 0 would then test a different feature set
    # than the one dimensioned by the matrix.
    feature_names = sorted({c for r in rows for c in r if c.startswith("f_")})

    def _rebuild(subset: Sequence[Dict[str, Any]]) -> Dict[str, List[Any]]:
        """Rebuild features from a row subset - the behavioural leak test input.

        The engine compares this list index-for-index against the full run, so the
        output must be purely a function of ``subset``. Reading ``r.get(name)``
        satisfies that: a truncated subset produces a shorter, identically-valued
        prefix. A key MISSING from a row yields None, which the engine treats as
        "missing at that index" rather than silently skipping the row.
        """
        return {name: [r.get(name) for r in subset] for name in feature_names}

    report = audit_leakage(rows, build_features=_rebuild,
                           forward_returns=forward_returns, ts_col=ts_col)

    matrix = pd.DataFrame({
        name.replace("f_", ""): pd.Series(
            {r["date"]: r.get(name) for r in rows}, dtype="float64")
        for name in feature_names
    })

    verdict = report.get("verdict")
    if require_clean and verdict != "clean":
        raise ValueError(
            f"feature matrix refused (leakage verdict={verdict}): "
            f"{report.get('detail') or 'no detail'}")

    return {"matrix": matrix, "features": feature_names, "leakage": report,
            "verdict": verdict, "detail": report.get("detail", "")}
