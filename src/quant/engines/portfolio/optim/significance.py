"""
optim/significance.py - is the difference real? (block bootstrap, pure).

The problem this solves
-----------------------
The benchmark prints a table with Sharpe 0.628 for HRP and 0.619 for inverse-vol,
and declares HRP the winner. **That is not a finding — it is a coin flip.** Two
strategies whose Sharpe differs by 0.01 over 145 periods are indistinguishable
from each other; the ranking is sampling noise.

Before switching a production optimiser on a 0.01 Sharpe difference, a desk
needs the error bars. That is what this module computes.

Method: stationary / circular **block bootstrap**
------------------------------------------------
Naive bootstrapping (resample individual days) destroys the autocorrelation and
volatility clustering that dominate financial returns, so it *understates* the
uncertainty and manufactures false confidence. The block bootstrap resamples
contiguous *blocks* of observations, preserving short-range dependence.

We use the **circular block bootstrap**: blocks wrap around the end of the
series, which uses all observations and avoids the edge bias of the stationary
variant. Block length defaults to ``~sqrt(n)`` (the standard rule of thumb:
long enough to capture dependence, short enough to give enough distinct blocks).

What we report
--------------
* A confidence interval per metric (Sharpe, annual return, max drawdown).
* **P(A beats B)** from a paired bootstrap — the number that actually answers
  "should I switch?".
* ``significant`` = True only when the CI excludes the comparison point.

Everything is pure and dependency-free.
"""
from __future__ import annotations

import logging
import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = ["block_indices", "bootstrap_metric", "bootstrap_ci",
           "paired_bootstrap", "compare_series", "MIN_BLOCKS_FOR_CONFIDENCE"]


#: Below this many blocks the bootstrap cannot resolve much; we warn.
MIN_BLOCKS_FOR_CONFIDENCE = 8


def block_indices(n: int, block: int, *, rng: random.Random
                  ) -> List[int]:
    """Circular block bootstrap index list of length ``n``.

    Draws ``ceil(n/block)`` contiguous blocks of length ``block`` with random
    starts, wrapping around the series (circular), then truncates to ``n``.
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
    """Rule-of-thumb block length: ``max(2, round(sqrt(n)))``."""
    return max(2, int(round(math.sqrt(max(n, 1)))))


def bootstrap_metric(returns: Sequence[float], metric: Any, *,
                     n_boot: int = 1000, block: Optional[int] = None,
                     seed: int = 12345, periods: float = 252.0) -> Dict[str, Any]:
    """Bootstrap the sampling distribution of one metric.

    ``metric`` is a callable ``f(sample, periods=...) -> float|None`` — pass one
    of the ``attribution.perf`` functions (``sharpe``, ``max_drawdown``, ...).

    Returns ``{"ok", "point", "mean", "se", "ci_low", "ci_high", "n_boot",
    "block", "warnings"}`` at the 95% level.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    n = len(rs)
    if n < 20:
        return {"ok": False, "point": None, "mean": None, "se": None,
                "ci_low": None, "ci_high": None, "n_boot": 0, "block": 0,
                "warnings": [f"need >= 20 observations, have {n}"]}
    warnings: List[str] = []
    blk = int(block) if block else _auto_block(n)
    if n / max(blk, 1) < MIN_BLOCKS_FOR_CONFIDENCE:
        warnings.append(
            f"only {n / blk:.1f} independent blocks; CI is indicative only")
    rng = random.Random(seed)
    point = _safe_metric(metric, rs, periods)
    samples: List[float] = []
    for _ in range(max(1, int(n_boot))):
        idx = block_indices(n, blk, rng=rng)
        sample = [rs[i] for i in idx]
        v = _safe_metric(metric, sample, periods)
        if v is not None:
            samples.append(v)
    if not samples:
        return {"ok": False, "point": point, "mean": None, "se": None,
                "ci_low": None, "ci_high": None, "n_boot": 0, "block": blk,
                "warnings": warnings + ["no bootstrap sample produced a metric"]}
    samples.sort()
    return {
        "ok": True,
        "point": point,
        "mean": mean(samples),
        "se": stdev(samples),
        "ci_low": _quantile(samples, 0.025),
        "ci_high": _quantile(samples, 0.975),
        "n_boot": len(samples),
        "block": blk,
        "warnings": warnings,
    }


def _safe_metric(metric: Any, sample: Sequence[float],
                 periods: float) -> Optional[float]:
    try:
        v = metric(sample, periods=periods)
    except TypeError:
        try:
            v = metric(sample)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    f = safe_float(v)
    return f if f is not None else None


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


def bootstrap_ci(returns: Sequence[float], *, metrics: Optional[Dict[str, Any]]
                 = None, n_boot: int = 1000, block: Optional[int] = None,
                 seed: int = 12345, periods: float = 252.0) -> Dict[str, Any]:
    """Confidence intervals for several metrics in one pass.

    ``metrics`` defaults to ``{"sharpe", "annual_return", "annual_vol",
    "max_drawdown"}`` from ``attribution.perf``. Returns ``{metric: {...}}``.
    """
    if metrics is None:
        from ..attribution import perf
        metrics = {
            "sharpe": perf.sharpe,
            "annual_return": perf.annualised_return,
            "annual_vol": perf.annualised_vol,
            "max_drawdown": perf.max_drawdown,
        }
    out: Dict[str, Any] = {}
    for name, fn in metrics.items():
        out[name] = bootstrap_metric(returns, fn, n_boot=n_boot, block=block,
                                     seed=seed, periods=periods)
    return out


def paired_bootstrap(returns_a: Sequence[float], returns_b: Sequence[float], *,
                     metric: Any, n_boot: int = 1000,
                     block: Optional[int] = None, seed: int = 12345,
                     periods: float = 252.0) -> Dict[str, Any]:
    """Paired block bootstrap of ``metric(A) - metric(B)``.

    Pairing matters: the two strategies trade the *same* market days, so their
    returns are highly correlated. Resampling the *same* indices for both
    preserves that, giving a much tighter (and honest) interval than comparing
    two independent CIs.

    Returns ``{"ok", "diff", "ci_low", "ci_high", "p_a_beats_b", "significant",
    "n_boot", "block", "warnings"}`` where ``p_a_beats_b`` is the fraction of
    bootstrap samples in which A beat B.
    """
    a = [safe_float(x) for x in (returns_a or [])]
    b = [safe_float(x) for x in (returns_b or [])]
    pair = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(pair)
    if n < 20:
        return {"ok": False, "diff": None, "ci_low": None, "ci_high": None,
                "p_a_beats_b": None, "significant": False, "n_boot": 0,
                "block": 0,
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
        sa = [xs[i] for i in idx]
        sb = [ys[i] for i in idx]
        va = _safe_metric(metric, sa, periods)
        vb = _safe_metric(metric, sb, periods)
        if va is None or vb is None:
            continue
        diffs.append(va - vb)
        if va > vb:
            wins += 1
    if not diffs:
        return {"ok": False, "diff": diff, "ci_low": None, "ci_high": None,
                "p_a_beats_b": None, "significant": False, "n_boot": 0,
                "block": blk, "warnings": warnings + ["no valid bootstrap pairs"]}
    diffs.sort()
    ci_low = _quantile(diffs, 0.025)
    ci_high = _quantile(diffs, 0.975)
    # Significant when the whole CI sits on one side of zero.
    significant = (ci_low is not None and ci_low > 0) or (
        ci_high is not None and ci_high < 0)
    return {
        "ok": True,
        "diff": diff,
        "mean_diff": mean(diffs),
        "ci_low": ci_low,
        "ci_high": ci_high,
        "p_a_beats_b": wins / len(diffs),
        "significant": bool(significant),
        "n_boot": len(diffs),
        "block": blk,
        "warnings": warnings,
    }


def compare_series(series_a: Sequence[float], series_b: Sequence[float], *,
                   label_a: str = "A", label_b: str = "B",
                   n_boot: int = 2000, seed: int = 12345,
                   periods: float = 252.0) -> Dict[str, Any]:
    """Full significance verdict for A vs B on Sharpe and max-drawdown.

    Returns ``{"ok", "label_a", "label_b", "sharpe": {...}, "max_drawdown":
    {...}, "verdict", "warnings"}``. ``verdict`` is a one-line, plain-English
    conclusion a PM can act on — including the honest "not distinguishable".
    """
    from ..attribution import perf

    sharpe = paired_bootstrap(series_a, series_b, metric=perf.sharpe,
                              n_boot=n_boot, seed=seed, periods=periods)
    mdd = paired_bootstrap(series_a, series_b, metric=perf.max_drawdown,
                           n_boot=n_boot, seed=seed, periods=periods)
    if not sharpe.get("ok"):
        return {"ok": False, "label_a": label_a, "label_b": label_b,
                "sharpe": sharpe, "max_drawdown": mdd,
                "verdict": "insufficient data for a significance test",
                "warnings": sharpe.get("warnings", [])}

    d = sharpe["diff"]
    p = sharpe["p_a_beats_b"]
    if sharpe["significant"]:
        better = label_a if d > 0 else label_b
        verdict = (f"{better} is significantly better (Sharpe diff "
                   f"{d:+.4f}, 95% CI "
                   f"[{sharpe['ci_low']:+.4f}, {sharpe['ci_high']:+.4f}], "
                   f"P(win)={p:.0%})")
    else:
        verdict = (f"NOT distinguishable: Sharpe diff {d:+.4f} with 95% CI "
                   f"[{sharpe['ci_low']:+.4f}, {sharpe['ci_high']:+.4f}] spans "
                   f"zero (P({label_a} wins)={p:.0%}). Do not switch on this "
                   f"evidence alone.")
    return {
        "ok": True, "label_a": label_a, "label_b": label_b,
        "sharpe": sharpe, "max_drawdown": mdd, "verdict": verdict,
        "warnings": sharpe.get("warnings", []) + mdd.get("warnings", []),
    }