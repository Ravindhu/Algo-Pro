"""
models/hmm_regime.py - Gaussian HMM regime posterior (fixes weaknesses #4, #5).

A single macro print should not flip the label instantly: real regimes are
*sticky*. A Hidden Markov Model with a handful of Gaussian states captures that
persistence and, crucially, gives a **posterior probability** per state - so the
engine can report ``P(RISK_ON)=0.72`` instead of a bare label.

Implementation note
-------------------
Rather than depend on ``hmmlearn`` (heavy, and a C build), this is a compact,
pure-``numpy`` **Gaussian HMM with Baum-Welch (EM) training and Viterbi/forward
decoding**. It is deliberately small and readable - a desk-trusted "good enough"
regime detector - and it degrades cleanly:

    * no numpy            -> ``method='unavailable'`` (engine keeps label logic)
    * too little data     -> ``method='insufficient_data'``
    * any numeric failure -> ``method='error'``

States are *labelled* post-hoc by the sign of their mean return, so state 0 is
not assumed to be "risk-on": we map the highest-mean state to RISK_ON and the
lowest to RISK_OFF, keeping the output semantically stable across fits.

Reproducibility: initialisation is deterministic (k-means++-style seeding from
sorted quantiles), no RNG, so the same input gives the same fit.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float

try:  # optional dependency
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAS_NUMPY = False


def _log_sum_exp(vals: Any) -> float:
    m = float(np.max(vals))
    return m + math.log(float(np.sum(np.exp(vals - m))))


def _gaussian_logpdf(X: Any, mu: Any, var: Any) -> Any:
    """Log N(x | mu, var) for a diagonal-covariance Gaussian, row-wise."""
    var = np.maximum(var, 1e-6)
    diff = X - mu
    return -0.5 * (np.log(2 * math.pi * var) + (diff ** 2) / var).sum(axis=1)


class GaussianHMM:
    """Minimal diagonal-covariance Gaussian HMM (EM training + forward decode).

    Parameters
    ----------
    n_states : number of hidden regimes (2-3 recommended: bear/neutral/bull).
    n_iter : EM iterations. Small on purpose - regime series are short.
    tol : convergence tolerance on the log-likelihood.
    """

    def __init__(self, n_states: int = 3, *, n_iter: int = 40,
                 tol: float = 1e-4, seed_quantiles: bool = True):
        self.n_states = int(n_states)
        self.n_iter = int(n_iter)
        self.tol = float(tol)
        self.seed_quantiles = seed_quantiles
        self.mu = None      # (K, D)
        self.var = None     # (K, D)
        self.startprob = None   # (K,)
        self.transmat = None    # (K, K)
        self.fitted = False

    # ---- initialisation ------------------------------------------------- #
    def _init(self, X: Any) -> None:
        T, D = X.shape
        K = self.n_states
        # Deterministic seeding: split the *first* feature's sorted order into
        # K contiguous buckets and use bucket means as initial state means.
        order = np.argsort(X[:, 0])
        buckets = np.array_split(order, K)
        self.mu = np.vstack([X[b].mean(axis=0) for b in buckets])
        # Global variance as a safe initial covariance.
        gvar = X.var(axis=0) + 1e-3
        self.var = np.tile(gvar, (K, 1))
        self.startprob = np.full(K, 1.0 / K)
        # Persistent diagonal-heavy transition matrix.
        tm = np.full((K, K), 0.1 / max(1, K - 1))
        np.fill_diagonal(tm, 0.9)
        self.transmat = tm / tm.sum(axis=1, keepdims=True)

    # ---- EM ------------------------------------------------------------- #
    def _forward_backward(self, X: Any):
        T = X.shape[0]
        K = self.n_states
        logB = np.vstack([_gaussian_logpdf(X, self.mu[k], self.var[k])
                          for k in range(K)]).T          # (T, K)
        logA = np.log(self.transmat + 1e-12)
        logpi = np.log(self.startprob + 1e-12)

        logalpha = np.zeros((T, K))
        logalpha[0] = logpi + logB[0]
        for t in range(1, T):
            for k in range(K):
                logalpha[t, k] = _log_sum_exp(logalpha[t - 1] + logA[:, k]) \
                    + logB[t, k]
        logbeta = np.zeros((T, K))
        for t in range(T - 2, -1, -1):
            for k in range(K):
                logbeta[t, k] = _log_sum_exp(
                    logA[k] + logB[t + 1] + logbeta[t + 1])
        ll = _log_sum_exp(logalpha[T - 1])
        gamma = np.exp(logalpha + logbeta - ll)
        return logalpha, logbeta, gamma, ll, logB, logA

    def fit(self, X: Any) -> "GaussianHMM":
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        if X.shape[0] < self.n_states * 3:
            self.fitted = False
            return self
        self._init(X)
        prev_ll = -np.inf
        for _ in range(self.n_iter):
            logalpha, logbeta, gamma, ll, logB, logA = \
                self._forward_backward(X)
            if abs(ll - prev_ll) < self.tol:
                prev_ll = ll
                break
            prev_ll = ll

            # M-step.
            T = X.shape[0]
            xi_sum = np.zeros((self.n_states, self.n_states))
            for t in range(T - 1):
                denom = logalpha[t][:, None] + logA + logB[t + 1][None, :] \
                    + logbeta[t + 1][None, :]
                m = float(np.max(denom))
                xi = np.exp(denom - m)
                xi_sum += xi / xi.sum()
            self.transmat = xi_sum / np.maximum(xi_sum.sum(axis=1, keepdims=True),
                                                1e-12)
            self.startprob = gamma[0] / gamma[0].sum()
            w = gamma.sum(axis=0) + 1e-12
            self.mu = (gamma.T @ X) / w[:, None]
            for k in range(self.n_states):
                diff = X - self.mu[k]
                self.var[k] = (gamma[:, k][:, None] * diff ** 2).sum(axis=0) / w[k]
            self.var = np.maximum(self.var, 1e-6)
        self.fitted = True
        self._loglik = float(prev_ll)
        return self

    def predict_proba(self, X: Any) -> Any:
        """Posterior state probabilities for each row (forward-decoded)."""
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        _, _, gamma, _, _, _ = self._forward_backward(X)
        return gamma

    def _state_means(self) -> Any:
        # Mean of the first feature per state (the composite risk-on score).
        return self.mu[:, 0]


def detect_regime(observations: Sequence[float], *, n_states: int = 3,
                  n_iter: int = 40) -> Dict[str, Any]:
    """Fit an HMM to a composite-score history and decode the current regime.

    Parameters
    ----------
    observations : time-ordered composite risk-on scores (float list).
    n_states : 2 (bear/bull) or 3 (bear/neutral/bull).

    Returns::

        {"method": "hmm"|"unavailable"|"insufficient_data"|"error",
         "state": int, "label": "RISK_ON"|"NEUTRAL"|"RISK_OFF",
         "probabilities": {"RISK_ON": p, ...}, "p_risk_on": float,
         "state_means": [...], "log_likelihood": float}

    State->label mapping is by *mean score*: the highest-mean state is RISK_ON,
    the lowest RISK_OFF, the middle (if any) NEUTRAL. This keeps the semantics
    stable even though HMM state indices are arbitrary.
    """
    if not _HAS_NUMPY:
        return {"method": "unavailable", "state": None, "label": None,
                "probabilities": {}, "p_risk_on": None}
    data = [safe_float(v) for v in observations]
    data = [v for v in data if v is not None]
    if len(data) < max(6, n_states * 3):
        return {"method": "insufficient_data", "state": None, "label": None,
                "probabilities": {}, "p_risk_on": None, "n": len(data)}
    try:
        X = np.asarray(data, dtype=float).reshape(-1, 1)
        hmm = GaussianHMM(n_states=n_states, n_iter=n_iter).fit(X)
        if not hmm.fitted:
            return {"method": "insufficient_data", "state": None, "label": None,
                    "probabilities": {}, "p_risk_on": None}
        gamma = hmm.predict_proba(X)
        posterior = gamma[-1]                      # current-state probabilities
        means = hmm._state_means()
        order = list(np.argsort(means))            # ascending mean score
        labels = {order[0]: "RISK_OFF"}
        if n_states == 2:
            labels[order[1]] = "RISK_ON"
        else:
            labels[order[0]] = "RISK_OFF"
            labels[order[-1]] = "RISK_ON"
            for mid in order[1:-1]:
                labels[int(mid)] = "NEUTRAL"

        prob_by_label: Dict[str, float] = {}
        for s, p in enumerate(posterior):
            lab = labels.get(int(s), "NEUTRAL")
            prob_by_label[lab] = prob_by_label.get(lab, 0.0) + float(p)
        state_star = int(np.argmax(posterior))
        return {
            "method": "hmm",
            "state": state_star,
            "label": labels.get(state_star, "NEUTRAL"),
            "probabilities": {k: round(v, 4) for k, v in prob_by_label.items()},
            "p_risk_on": round(prob_by_label.get("RISK_ON", 0.0), 4),
            "state_means": [round(float(m), 6) for m in means],
            "log_likelihood": round(float(getattr(hmm, "_loglik", 0.0)), 4),
        }
    except Exception as exc:  # noqa: BLE001
        return {"method": "error", "error": repr(exc), "state": None,
                "label": None, "probabilities": {}, "p_risk_on": None}


def available() -> bool:
    return _HAS_NUMPY


__all__ = ["GaussianHMM", "detect_regime", "available"]