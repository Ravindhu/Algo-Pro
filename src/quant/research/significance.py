"""Significance testing: circular block bootstrap for metric differences.

Flow stage: Research validation (DSR/PBO support)
Status: PHASE 3 IMPLEMENTED (2026-11-11, decoupling stage 6.4 part 1)
Ported from src/quant/blue_engine/portfolio/optim/significance.py (pure stdlib); metric
functions come from quant.research.perf_metrics (local since stage 1).

Why it exists: a Sharpe difference of 0.01 over 145 periods is a coin flip.
This module puts error bars on the comparison: a 95% CI per metric and
P(A beats B) from a PAIRED bootstrap (same block indices for both series, so
the shared market days stay correlated). ``significant`` is True only when the
whole CI sits on one side of zero.

Method: circular block bootstrap — blocks wrap around the series, preserving
the short-range dependence that iid resampling destroys. Block length defaults
to ~sqrt(n). Pure and deterministic for a fixed seed. No I/O, no orders.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Dict, List, Optional, Sequence

from quant.research.perf_metrics import max_drawdown, sharpe

__all__ = ["block_indices", "bootstrap_metric", "bootstrap_ci",
           "paired_bootstrap", "compare_series", "MIN_BLOCKS_FOR_CONFIDENCE"]

#: Below this many blocks the bootstrap cannot resolve much; we warn.
MIN_BLOCKS_FOR_CONFIDENCE = 8


def _clean(xs: Sequence[float]) -> List[float]:
    out = []
    for x in xs or ():
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v == v:  # not NaN
            out.append(v)
    return out


def block_indices(n: int, block: int, *, rng: random.Random) -> List[int]:
    """Circular block bootstrap index list of length ``n``.

    Draws ceil(n/block) contiguous blocks of length ``block`` with random starts,
    wrapping around the series, then truncates to ``n``.
    """
    if n <= 0:
        return []
    if block <= 1:
        return [rng.randrange(n) for _ in range(n)]
    block = min(block, n)
    n_blocks = int(math.ceil(n / block))
    out: List[int] = []
    for _ in range(n_blocks):
        start = rng.randrange(n)
        for k in range(block):
            out.append((start + k) % n)
            if len(out) >= n:
                return out
    return out[:n]


def _auto_block(n: int) -> int:
    """Rule-of-thumb block length: max(2, round(sqrt(n)))."""
    return max(2, int(round(math.sqrt(max(n, 1)))))


def _perf_sharpe(sample: Sequence[float], periods: float) -> Optional[float]:
    """quant.perf_metrics.sharpe signature shim (periods_per_year, not periods)."""
    return sharpe(sample, periods_per_year=periods)


def _perf_max_drawdown(sample: Sequence[float], periods: float) -> Optional[float]:
    """Drawdown is scale-free; the bootstrap passes returns as the equity path."""
    return max_drawdown(sample)


def _safe_metric(metric: Callable[..., Optional[float]], sample: Sequence[float],
                 periods: float) -> Optional[float]:
    try:
        return metric(sample, periods=periods)
    except Exception:
        return None


def _quantile(sorted_xs: Sequence[float], q: float) -> Optional[float]:
    n = len(sorted_xs)
    if n == 0:
        return None
    if n == 1:
        return sorted_xs[0]
    pos = q * (n - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] * (1 - (pos - lo)) + sorted_xs[hi] * (pos - lo)


def _mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _stdev(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def bootstrap_metric(returns: Sequence[float], metric: Callable[..., Optional[float]], *,
                     n_boot: int = 1000, block: Optional[int] = None,
                     seed: int = 12345, periods: float = 252.0) -> Dict[str, Any]:
    """Bootstrap the sampling distribution of one metric (95% CI)."""
    rs = _clean(returns)
    n = len(rs)
    if n < 20:
        return {"ok": False, "point": None, "mean": None, "se": None,
                "ci_low": None, "ci_high": None, "n_boot": 0, "block": 0,
                "warnings": [f"need >= 20 observations, have {n}"]}
    warnings: List[str] = []
    blk = int(block) if block else _auto_block(n)
    if n / max(blk, 1) < MIN_BLOCKS_FOR_CONFIDENCE:
        warnings.append(f"only {n / blk:.1f} independent blocks; CI is indicative only")
    rng = random.Random(seed)
    point = _safe_metric(metric, rs, periods)
    samples: List[float] = []
    for _ in range(max(1, int(n_boot))):
        sample = [rs[i] for i in block_indices(n, blk, rng=rng)]
        v = _safe_metric(metric, sample, periods)
        if v is not None:
            samples.append(v)
    if not samples:
        return {"ok": False, "point": point, "mean": None, "se": None,
                "ci_low": None, "ci_high": None, "n_boot": 0, "block": blk,
                "warnings": warnings + ["no bootstrap sample produced a metric"]}
    samples.sort()
    return {"ok": True, "point": point, "mean": _mean(samples), "se": _stdev(samples),
            "ci_low": _quantile(samples, 0.025), "ci_high": _quantile(samples, 0.975),
            "n_boot": len(samples), "block": blk, "warnings": warnings}


def bootstrap_ci(returns: Sequence[float], *, metrics: Optional[Dict[str, Any]] = None,
                 n_boot: int = 1000, block: Optional[int] = None,
                 seed: int = 12345, periods: float = 252.0) -> Dict[str, Any]:
    """Confidence intervals for several metrics in one pass. Defaults: sharpe, mdd."""
    if metrics is None:
        metrics = {"sharpe": _perf_sharpe, "max_drawdown": _perf_max_drawdown}
    return {name: bootstrap_metric(returns, fn, n_boot=n_boot, block=block,
                                   seed=seed, periods=periods)
            for name, fn in metrics.items()}


def paired_bootstrap(returns_a: Sequence[float], returns_b: Sequence[float], *,
                     metric: Callable[..., Optional[float]], n_boot: int = 1000,
                     block: Optional[int] = None, seed: int = 12345,
                     periods: float = 252.0) -> Dict[str, Any]:
    """Paired block bootstrap of metric(A) - metric(B).

    Pairing matters: both strategies trade the same market days, so resampling the
    SAME indices for both preserves that correlation and gives an honest interval.
    """
    a = _clean(returns_a)
    b = _clean(returns_b)
    pair = [(x, y) for x, y in zip(a, b)]
    n = len(pair)
    if n < 20:
        return {"ok": False, "diff": None, "ci_low": None, "ci_high": None,
                "p_a_beats_b": None, "significant": False, "n_boot": 0, "block": 0,
                "warnings": [f"need >= 20 paired observations, have {n}"]}
    warnings: List[str] = []
    blk = int(block) if block else _auto_block(n)
    rng = random.Random(seed)
    xs = [p[0] for p in pair]
    ys = [p[1] for p in pair]
    point_a = _safe_metric(metric, xs, periods)
    point_b = _safe_metric(metric, ys, periods)
    diff = None if (point_a is None or point_b is None) else point_a - point_b

    diffs: List[float] = []
    wins = 0
    for _ in range(max(1, int(n_boot))):
        idx = block_indices(n, blk, rng=rng)
        va = _safe_metric(metric, [xs[i] for i in idx], periods)
        vb = _safe_metric(metric, [ys[i] for i in idx], periods)
        if va is None or vb is None:
            continue
        diffs.append(va - vb)
        if va > vb:
            wins += 1
    if not diffs:
        return {"ok": False, "diff": diff, "ci_low": None, "ci_high": None,
                "p_a_beats_b": None, "significant": False, "n_boot": 0, "block": blk,
                "warnings": warnings + ["no valid bootstrap pairs"]}
    diffs.sort()
    ci_low, ci_high = _quantile(diffs, 0.025), _quantile(diffs, 0.975)
    significant = (ci_low is not None and ci_low > 0) or (ci_high is not None and ci_high < 0)
    return {"ok": True, "diff": diff, "mean_diff": _mean(diffs),
            "ci_low": ci_low, "ci_high": ci_high, "p_a_beats_b": wins / len(diffs),
            "significant": bool(significant), "n_boot": len(diffs), "block": blk,
            "warnings": warnings}


def compare_series(series_a: Sequence[float], series_b: Sequence[float], *,
                   label_a: str = "A", label_b: str = "B", n_boot: int = 2000,
                   seed: int = 12345, periods: float = 252.0) -> Dict[str, Any]:
    """Full significance verdict for A vs B on Sharpe and max-drawdown.

    ``verdict`` is a one-line, plain-English conclusion — including the honest
    "NOT distinguishable".
    """
    sharpe_res = paired_bootstrap(series_a, series_b, metric=_perf_sharpe,
                                  n_boot=n_boot, seed=seed, periods=periods)
    mdd_res = paired_bootstrap(series_a, series_b, metric=_perf_max_drawdown,
                               n_boot=n_boot, seed=seed, periods=periods)
    if not sharpe_res.get("ok"):
        return {"ok": False, "label_a": label_a, "label_b": label_b,
                "sharpe": sharpe_res, "max_drawdown": mdd_res,
                "verdict": "insufficient data for a significance test",
                "warnings": sharpe_res.get("warnings", [])}
    d = sharpe_res["diff"]
    p = sharpe_res["p_a_beats_b"]
    if sharpe_res["significant"]:
        better = label_a if d > 0 else label_b
        verdict = (f"{better} is significantly better (Sharpe diff {d:+.4f}, 95% CI "
                   f"[{sharpe_res['ci_low']:+.4f}, {sharpe_res['ci_high']:+.4f}], "
                   f"P(win)={p:.0%})")
    else:
        verdict = (f"NOT distinguishable: Sharpe diff {d:+.4f} with 95% CI "
                   f"[{sharpe_res['ci_low']:+.4f}, {sharpe_res['ci_high']:+.4f}] spans "
                   f"zero (P({label_a} wins)={p:.0%}). Do not switch on this evidence alone.")
    return {"ok": True, "label_a": label_a, "label_b": label_b,
            "sharpe": sharpe_res, "max_drawdown": mdd_res, "verdict": verdict,
            "warnings": sharpe_res.get("warnings", []) + mdd_res.get("warnings", [])}
