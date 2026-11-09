"""Overfitting checks: Sharpe, Deflated Sharpe (DSR), Probability of Backtest Overfitting (PBO), bootstrap.

Flow stage: Trial logger + DSR/PBO, Bootstrap of trades
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/ml/evaluation/metrics.py (sharpe: wrapped).
DSR, PBO and bootstrap are kept here on purpose. See docs/decisions/0001-dsr-pbo-bootstrap-kept-local.md.
"""
from __future__ import annotations

import itertools
import math

import numpy as np
from scipy.stats import kurtosis, norm, rankdata, skew

EULER_GAMMA = 0.5772156649015329


def sharpe(returns, periods: int = 252) -> float:
    """Annualised Sharpe. Delegates to Blue_engine; 0.0 when undefined (kept from the old contract)."""
    from quant.research.perf_metrics import sharpe as _sharpe

    value = _sharpe(list(returns), periods_per_year=float(periods))
    return 0.0 if value is None else float(value)


def expected_max_sharpe(n_trials: int, trial_sharpe_var: float) -> float:
    """Expected best Sharpe from n_trials unskilled trials (per-period units)."""
    if n_trials <= 1:
        return 0.0
    return math.sqrt(trial_sharpe_var) * (
        (1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials)
        + EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * math.e))
    )


def deflated_sharpe(returns, n_trials: int, trial_sharpe_var: float) -> float:
    """Probability that the true Sharpe is above the best-of-n_trials luck level (Bailey and Lopez de Prado)."""
    r = np.asarray(returns, dtype=float)
    T = len(r)
    if T < 3 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)
    g3 = float(skew(r))
    g4 = float(kurtosis(r, fisher=False))
    sr0 = expected_max_sharpe(n_trials, trial_sharpe_var)
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    return float(norm.cdf(z))


def prob_backtest_overfit(matrix, n_blocks: int = 8) -> float:
    """PBO by combinatorially symmetric cross-validation. matrix: rows = time, columns = trials."""
    M = np.asarray(matrix, dtype=float)
    T, N = M.shape
    if n_blocks % 2 or T < n_blocks or N < 2:
        raise ValueError("need an even n_blocks, T >= n_blocks and at least 2 trials")
    blocks = np.array_split(np.arange(T), n_blocks)
    logits = []
    for combo in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(n_blocks) if i not in combo])
        perf_is = _mean_over_std(M[is_idx])
        perf_oos = _mean_over_std(M[oos_idx])
        best = int(np.argmax(perf_is))
        rank = rankdata(perf_oos)[best] / (N + 1)
        logits.append(math.log(rank / (1 - rank)))
    return float(np.mean(np.asarray(logits) <= 0))


def _mean_over_std(X: np.ndarray) -> np.ndarray:
    s = X.std(axis=0, ddof=1)
    s = np.where(s == 0, np.nan, s)
    return np.nan_to_num(X.mean(axis=0) / s, nan=-np.inf)


def bootstrap_mean_pvalue(trade_returns, n_boot: int = 10_000, seed: int = 0) -> float:
    """One-sided p-value that the mean trade return is above zero (centered bootstrap)."""
    r = np.asarray(trade_returns, dtype=float)
    if len(r) < 2:
        return 1.0
    rng = np.random.default_rng(seed)
    centered = r - r.mean()
    idx = rng.integers(0, len(r), size=(n_boot, len(r)))
    boot_means = centered[idx].mean(axis=1)
    return float(np.mean(boot_means >= r.mean()))
