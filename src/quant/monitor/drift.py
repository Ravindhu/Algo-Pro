"""Population Stability Index (PSI) on features. Above 0.25 is a serious shift.

Flow stage: Data drift monitor
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/ml/audit/degradation.py
"""
from __future__ import annotations

import numpy as np


def psi(expected, actual, bins: int = 10) -> float:
    e = np.asarray(expected, float)
    a = np.asarray(actual, float)
    edges = np.quantile(e, np.linspace(0, 1, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    pe = np.histogram(e, edges)[0] / len(e)
    pa = np.histogram(a, edges)[0] / len(a)
    pe, pa = np.clip(pe, 1e-6, None), np.clip(pa, 1e-6, None)
    return float(np.sum((pa - pe) * np.log(pa / pe)))
