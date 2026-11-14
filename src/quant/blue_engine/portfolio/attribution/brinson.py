"""
attribution/brinson.py - Brinson-Fachler performance attribution (pure).

Decomposes active return (portfolio vs benchmark) into three effects per sleeve::

    Allocation  A_i = (w_i - b_i) * (r_i^bench - R_bench)
    Selection   S_i = b_i         * (r_i^port  - r_i^bench)
    Interaction I_i = (w_i - b_i) * (r_i^port  - r_i^bench)
    Total       = A_i + S_i + I_i     (sums to the active return)

*Allocation* asks "were we overweight the right sleeves?"; *selection* asks "did
our names inside a sleeve beat the sleeve benchmark?"; *interaction* is the
cross term. This is the standard Brinson-Fachler form with the benchmark's
sleeve weights as the base — the most defensible variant for a desk that can
choose both sleeve weights and names.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ...core.results import clamp, safe_float

logger = logging.getLogger(__name__)

__all__ = ["brinson_attribution", "active_return"]


def brinson_attribution(weights: Dict[str, float], bench_weights: Dict[str, float],
                        sleeve_returns: Dict[str, float],
                        bench_sleeve_returns: Dict[str, float]) -> Dict[str, Any]:
    """Brinson-Fachler attribution.

    All returns are *period* returns (fractions). Weights are normalised to sum 1
    so a partial book does not distort the arithmetic. Returns per-sleeve
    ``allocation`` / ``selection`` / ``interaction`` / ``total`` plus the
    aggregates and the portfolio/benchmark total returns.
    """
    sleeves = set(weights) | set(bench_weights) | set(sleeve_returns) \
        | set(bench_sleeve_returns)
    w_tot = sum(abs(safe_float(v) or 0.0) for v in weights.values()) or 1.0
    b_tot = sum(abs(safe_float(v) or 0.0) for v in bench_weights.values()) or 1.0

    rows: Dict[str, Dict[str, float]] = {}
    R_bench = 0.0
    R_port = 0.0
    for s in sleeves:
        w = (safe_float(weights.get(s)) or 0.0) / w_tot
        b = (safe_float(bench_weights.get(s)) or 0.0) / b_tot
        rp = safe_float(sleeve_returns.get(s)) or 0.0
        rb = safe_float(bench_sleeve_returns.get(s)) or 0.0
        R_bench += b * rb
        R_port += w * rp
    for s in sleeves:
        w = (safe_float(weights.get(s)) or 0.0) / w_tot
        b = (safe_float(bench_weights.get(s)) or 0.0) / b_tot
        rp = safe_float(sleeve_returns.get(s)) or 0.0
        rb = safe_float(bench_sleeve_returns.get(s)) or 0.0
        alloc = (w - b) * (rb - R_bench)
        select = b * (rp - rb)
        inter = (w - b) * (rp - rb)
        rows[s] = {
            "weight": round(w, 6), "bench_weight": round(b, 6),
            "return": round(rp, 6), "bench_return": round(rb, 6),
            "allocation": round(alloc, 8), "selection": round(select, 8),
            "interaction": round(inter, 8),
            "total": round(alloc + select + inter, 8),
        }
    return {
        "ok": True,
        "portfolio_return": round(R_port, 8),
        "benchmark_return": round(R_bench, 8),
        "active_return": round(R_port - R_bench, 8),
        "allocation": round(sum(r["allocation"] for r in rows.values()), 8),
        "selection": round(sum(r["selection"] for r in rows.values()), 8),
        "interaction": round(sum(r["interaction"] for r in rows.values()), 8),
        "sleeves": rows,
    }


def active_return(weights: Dict[str, float], sleeve_returns: Dict[str, float],
                  bench_weights: Dict[str, float],
                  bench_sleeve_returns: Dict[str, float]) -> Optional[float]:
    """Simple active return (portfolio total - benchmark total)."""
    r = brinson_attribution(weights, bench_weights, sleeve_returns,
                            bench_sleeve_returns)
    return r.get("active_return")