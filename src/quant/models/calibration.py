"""Platt scaling and a shared logistic fitter. Maps raw scores to probabilities.

Flow stage: Calibrated conviction
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/models/base.py
NOTE: Platt only. Isotonic is a later option.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np


def fit_logistic(X, y, l2: float = 1e-3, lr: float = 0.1, iters: int = 2000) -> Tuple[np.ndarray, float]:
    X = np.asarray(X, float).reshape(len(y), -1)
    y = np.asarray(y, float)
    w, b = np.zeros(X.shape[1]), 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        g = X.T @ (p - y) / len(y) + l2 * w
        w -= lr * g
        b -= lr * np.mean(p - y)
    return w, b


def predict_proba(X, w, b) -> np.ndarray:
    X = np.asarray(X, float).reshape(len(X), -1)
    return 1 / (1 + np.exp(-(X @ w + b)))


def platt_fit(scores, labels) -> Tuple[np.ndarray, float]:
    s = np.asarray(scores, float)
    s = (s - s.mean()) / (s.std() or 1.0)
    return fit_logistic(s, labels)


def reliability(probs, labels, bins: int = 5):
    p, y = np.asarray(probs, float), np.asarray(labels, float)
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.any():
            rows.append((float(p[m].mean()), float(y[m].mean()), int(m.sum())))
    return rows
