"""
attribution/tearsheet.py — the M2 portfolio tearsheet (pure, no I/O).

A "tearsheet" is the one-page summary a PM hands the desk every morning.
This module builds it from the *stored history* in ``portfolio_state`` so it
works offline and is fully reproducible:

    snapshots  : [{"ts": ..., "target": {...}, "actual": {...}}, ...]  (drift)
    run rows   : [{"ts": ..., "gross": 1.3, "turnover": 0.04, "cost_bps": 4,
                   "regime": "RISK_ON", "optimiser": "hrp", ...}, ...]

What it computes (all degrade to ``None``/empty when data is missing):

* **weight_history**  — per-sleeve target & actual weight time series
  (feeds the Allocation tab's stacked lines).
* **turnover_series** — L1 turnover between *consecutive targets* (how much
  the optimiser wanted to trade each run) and actual-vs-target turnover.
* **drift_series**    — per-sleeve (actual - target) over time, plus the
  average/max total-abs-drift path.
* **hhi_series**      — concentration (Herfindahl) of the target book per run.
* **gross_series**    — gross exposure / cash weight path over runs.
* **rolling_stats**   — rolling Sharpe/vol of a *returns* list when one is
  supplied (live P&L attribution arrives in M6; today it is optional).
* **build()**         — one call that assembles the full tearsheet dict.

Everything is pure: no network, no SDK, no filesystem — the caller reads
``portfolio_state`` and passes the rows in. That keeps this module trivially
unit-testable (see ``tests/test_tearsheet.py``).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float
from . import drift as _drift

_log = logging.getLogger(__name__)

__all__ = ["weight_history", "turnover_series", "drift_history",
           "hhi_series", "gross_series", "rolling_stats", "build"]


def _hhi(weights: Dict[str, float]) -> Optional[float]:
    """Herfindahl index of a weight dict (0..1; 1 = single-sleeve book)."""
    vals = [abs(safe_float(v) or 0.0) for v in (weights or {}).values()]
    total = sum(vals)
    if total <= 0:
        return None
    return round(sum((v / total) ** 2 for v in vals), 6)


def _l1(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Half-L1 distance between two weight dicts = one-sided turnover."""
    keys = set(a) | set(b)
    return sum(abs((safe_float(a.get(k)) or 0.0)
                   - (safe_float(b.get(k)) or 0.0)) for k in keys) / 2.0


def weight_history(snapshots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-sleeve weight time series from drift snapshots.

    Returns ``{"ts": [...], "target": {sleeve: [...]}, "actual": {sleeve: [...]}}``
    — the exact shape a Plotly line chart wants.
    """
    ts: List[float] = []
    tgt: Dict[str, List[Optional[float]]] = {}
    act: Dict[str, List[Optional[float]]] = {}
    sleeves: List[str] = []
    for s in snapshots or []:
        ts.append(safe_float(s.get("ts")) or 0.0)
        for name, book, out in ((s.get("target") or {}, "target", tgt),
                                (s.get("actual") or {}, "actual", act)):
            for k in name:
                if k not in sleeves:
                    sleeves.append(k)
        # backfill every known sleeve so series stay aligned per row
        for k in sleeves:
            tgt.setdefault(k, []).append(safe_float((s.get("target") or {}).get(k)))
            act.setdefault(k, []).append(safe_float((s.get("actual") or {}).get(k)))
    # Leading-pad: sleeves discovered in later rows get None for earlier rows
    # so every series has exactly len(ts) points (Plotly x/y must align).
    n = len(ts)
    for v in list(tgt.values()) + list(act.values()):
        missing = n - len(v)
        if missing > 0:
            v[:0] = [None] * missing
    return {"ts": ts, "target": tgt, "actual": act, "sleeves": sleeves}


def turnover_series(snapshots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Turnover between consecutive *target* books, and target-vs-actual.

    Returns ``{"ts": [...], "planned": [...], "tracking": [...]}`` where
    ``planned[i]`` = L1(target[i], target[i-1]) — the rebalance the optimiser
    asked for — and ``tracking[i]`` = L1(actual[i], target[i]) — what is left
    to trade right now.
    """
    ts: List[float] = []
    planned: List[Optional[float]] = []
    tracking: List[Optional[float]] = []
    prev: Optional[Dict[str, float]] = None
    for s in snapshots or []:
        ts.append(safe_float(s.get("ts")) or 0.0)
        tgt = s.get("target") or {}
        act = s.get("actual") or {}
        planned.append(_l1(tgt, prev) if prev is not None else None)
        prev = tgt
        tracking.append(_l1(act, tgt) if (act or tgt) else None)
    return {"ts": ts, "planned": planned, "tracking": tracking}


def drift_history(snapshots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Total-abs-drift path + per-sleeve drift series (wraps ``drift.drift_series``)."""
    ts: List[float] = []
    total: List[Optional[float]] = []
    max_d: List[Optional[float]] = []
    for s in snapshots or []:
        ts.append(safe_float(s.get("ts")) or 0.0)
        d = _drift.drift(s.get("target") or {}, s.get("actual") or {})
        total.append(d.get("total_abs_drift"))
        max_d.append(d.get("max_drift"))
    return {"ts": ts, "total_abs_drift": total, "max_drift": max_d,
            "per_sleeve": _drift.drift_series(snapshots or [])}


def hhi_series(snapshots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Concentration of the target book over time (higher = more concentrated)."""
    ts: List[float] = []
    vals: List[Optional[float]] = []
    for s in snapshots or []:
        ts.append(safe_float(s.get("ts")) or 0.0)
        vals.append(_hhi(s.get("target") or {}))
    return {"ts": ts, "hhi": vals}


def gross_series(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Gross exposure / cash weight / cost path from stored *run rows*."""
    ts: List[float] = []
    gross: List[Optional[float]] = []
    cash: List[Optional[float]] = []
    cost: List[Optional[float]] = []
    to: List[Optional[float]] = []
    for r in rows or []:
        ts.append(safe_float(r.get("ts")) or 0.0)
        gross.append(safe_float(r.get("gross")))
        cash.append(safe_float(r.get("cash_weight")))
        cost.append(safe_float(r.get("cost_bps")))
        to.append(safe_float(r.get("turnover")))
    return {"ts": ts, "gross": gross, "cash_weight": cash,
            "cost_bps": cost, "turnover": to}


def rolling_stats(returns: Optional[Sequence[float]], window: int = 20,
                  periods_per_year: int = 252) -> Dict[str, Any]:
    """Rolling annualised vol & Sharpe over a returns list (daily returns).

    Degrades to empty series when ``returns`` is None/short. Pure stdlib.
    """
    if not returns or len(returns) < max(3, window):
        return {"ok": False, "window": window, "ts_idx": [], "vol": [],
                "sharpe": []}
    idx = list(range(window - 1, len(returns)))
    vols: List[float] = []
    sharpes: List[float] = []
    for i in idx:
        w = [safe_float(r) or 0.0 for r in returns[i - window + 1:i + 1]]
        n = len(w)
        mean = sum(w) / n
        var = sum((x - mean) ** 2 for x in w) / max(1, n - 1)
        vol = math.sqrt(var)
        ann_vol = vol * math.sqrt(periods_per_year)
        ann_ret = mean * periods_per_year
        vols.append(round(ann_vol, 6))
        sharpes.append(round((ann_ret / ann_vol) if ann_vol > 1e-12 else None
                             or 0.0, 4))
    return {"ok": True, "window": window, "ts_idx": idx,
            "vol": vols, "sharpe": sharpes}


def build(snapshots: Sequence[Dict[str, Any]],
          rows: Optional[Sequence[Dict[str, Any]]] = None,
          returns: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Assemble the full tearsheet in one dict (the dashboard's single source).

    Never raises — any missing piece degrades to ``None``/empty lists.
    """
    snaps = list(snapshots or [])
    runs = list(rows or [])
    try:
        wh = weight_history(snaps)
        to = turnover_series(snaps)
        dr = drift_history(snaps)
        hh = hhi_series(snaps)
        gr = gross_series(runs)
        rs = rolling_stats(returns)
        drift_vals = [v for v in (dr["total_abs_drift"] or []) if v is not None]
        hhi_vals = [v for v in (hh["hhi"] or []) if v is not None]
        to_vals = [v for v in (to["tracking"] or []) if v is not None]
        return {
            "ok": True,
            "runs": len(snaps),
            "weight_history": wh,
            "turnover": to,
            "drift": dr,
            "hhi": hh,
            "gross": gr,
            "rolling": rs,
            "summary": {
                "avg_total_abs_drift": (round(sum(drift_vals) / len(drift_vals), 6)
                                        if drift_vals else None),
                "max_total_abs_drift": (max(drift_vals) if drift_vals else None),
                "avg_hhi": (round(sum(hhi_vals) / len(hhi_vals), 6)
                            if hhi_vals else None),
                "avg_tracking_turnover": (round(sum(to_vals) / len(to_vals), 6)
                                          if to_vals else None),
                "last_target": (snaps[-1].get("target") or {}) if snaps else {},
                "last_actual": (snaps[-1].get("actual") or {}) if snaps else {},
            },
        }
    except Exception as exc:  # noqa: BLE001 - tearsheet must never abort a page
        _log.warning("tearsheet build failed: %s", exc)
        return {"ok": False, "runs": 0, "warnings": [f"tearsheet error: {exc}"]}
