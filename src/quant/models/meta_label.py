"""Meta-labeler: take or skip a signal, and size the bet from its calibrated probability.

Flow stage: Meta-labeler
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/alpha/confirm/state.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

from quant.models.calibration import fit_logistic, predict_proba


@dataclass
class MetaLabeler:
    w: np.ndarray
    b: float
    take_threshold: float = 0.55
    max_bet: float = 0.10

    @classmethod
    def fit(cls, features, took_win, take_threshold: float = 0.55, max_bet: float = 0.10) -> "MetaLabeler":
        w, b = fit_logistic(features, took_win)
        return cls(w, b, take_threshold, max_bet)

    def decide(self, features) -> Tuple[bool, float]:
        """(take, bet_fraction). Symmetric payoff Kelly: f = 2p - 1, capped."""
        p = float(predict_proba(np.atleast_2d(features), self.w, self.b)[0])
        take = p >= self.take_threshold
        return take, float(min(self.max_bet, max(0.0, 2 * p - 1))) if take else 0.0
