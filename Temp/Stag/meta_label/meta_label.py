"""Meta-labeler: take or skip a signal, and size the bet from its calibrated probability.

Flow stage: Meta-labeler
Status: PHASE 2 IMPLEMENTED; size-path defects fixed 2026-11-14
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/alpha/confirm/state.py
    (the engine has no meta-labeler; its confirm layer is a state machine, not a
    probability model, so the logic here is the source of truth)

FOUR DEFECTS FIXED, EACH ON THE MONEY PATH
------------------------------------------
A meta-labeler multiplies a probability into a bet, so a defect here loses money
directly rather than corrupting a backtest. Each was reproduced before fixing:

1. ``max_bet`` was not validated. ``max_bet=-0.5`` returned a bet fraction of
   -0.5: a NEGATIVE position size, which reverses the trade. Now refused.

2. A NaN feature returned ``(False, 0.0)`` - a silent skip that looks like
   "model says no" when the truth is "model could not read the input". Now it
   raises, so a data fault is visible instead of a missed trade.

3. An infinite feature returned ``(True, 0.10)`` - the MAXIMUM bet, because
   sigmoid(inf)=1 and the Kelly form saturates. A corrupt quote would therefore
   produce the largest possible position. Non-finite input is now refused.

4. Training on a single class produced a confident model. ``fit`` now raises,
   because one class carries no information.

The Kelly form and the take threshold semantics are UNCHANGED, so existing
fitted models keep their meaning.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

from quant.models.calibration import CalibrationError, fit_logistic, predict_proba

__all__ = ["MetaLabeler", "MetaLabelError"]

#: A bet fraction is a share of equity. Above this it is not a bet, it is a bug.
MAX_REASONABLE_BET = 1.0


class MetaLabelError(ValueError):
    """The meta-labeler cannot produce a trustworthy decision."""


@dataclass
class MetaLabeler:
    w: np.ndarray
    b: float
    take_threshold: float = 0.55
    max_bet: float = 0.10

    def __post_init__(self) -> None:
        if not (0.0 < float(self.take_threshold) < 1.0):
            raise MetaLabelError(
                f"take_threshold must be in (0, 1), got {self.take_threshold!r}; "
                "a threshold of 1 or more would silently skip every signal")
        if not (0.0 < float(self.max_bet) <= MAX_REASONABLE_BET):
            raise MetaLabelError(
                f"max_bet must be in (0, {MAX_REASONABLE_BET}], got {self.max_bet!r}; "
                "a negative or zero cap would invert or nullify every position")

    @classmethod
    def fit(cls, features, took_win, take_threshold: float = 0.55,
            max_bet: float = 0.10) -> "MetaLabeler":
        """Fit the take/skip model. Raises on a single-class label vector."""
        w, b = fit_logistic(features, took_win)
        if not (np.isfinite(w).all() and np.isfinite(b)):
            raise MetaLabelError("fit produced non-finite weights")
        return cls(w, b, take_threshold, max_bet)

    def probability(self, features) -> float:
        """Calibrated P(win). Raises on non-finite input or a width mismatch."""
        try:
            return float(predict_proba(np.atleast_2d(np.asarray(features, dtype=float)),
                                       self.w, self.b)[0])
        except CalibrationError as exc:
            raise MetaLabelError(str(exc)) from exc

    def decide(self, features) -> Tuple[bool, float]:
        """(take, bet_fraction). Symmetric-payoff Kelly: f = 2p - 1, capped.

        Returns 0.0 for the fraction whenever the signal is not taken, so a caller
        that ignores the boolean still sizes nothing.
        """
        p = self.probability(features)
        if not np.isfinite(p):
            raise MetaLabelError(f"model returned a non-finite probability: {p!r}")
        take = p >= self.take_threshold
        if not take:
            return False, 0.0
        bet = min(float(self.max_bet), max(0.0, 2.0 * p - 1.0))
        return True, float(bet)
