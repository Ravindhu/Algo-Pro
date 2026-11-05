"""Conviction score: combines six independent conditions into one score in [0, 1].

Flow stage: Calibrated conviction
Status: PHASE 2 IMPLEMENTED (step A1 score only; size tiers A2, caps A3 and calibration report A4 pending).
Pure function, no I/O, no broker or LLM calls.

Design rules:
- A weighted GEOMETRIC mean is used, not an arithmetic mean. One weak condition
  pulls the score down hard, so a high score needs most conditions to agree.
- A missing condition (None or NaN) counts as 0. Fail closed: missing data
  never adds conviction.
- An out-of-range value (below 0 or above 1) raises ValueError. That is a bug
  in the caller and must not be hidden.
- The score only ranks opportunities. It never sets size on its own; the
  tier mapping (step A2) and the hard caps (step A3) decide size.
- Weights are fixed in config. They are never fitted on the test data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional

# The six required conditions, in a fixed order. Keys are stable identifiers
# used in config and in audit records. Do not rename them casually.
CONDITIONS: tuple[str, ...] = (
    "momentum",          # 1. Technical momentum
    "trend_structure",   # 2. Market structure and trend alignment
    "catalyst",          # 3. Corporate catalyst / fundamental announcement quality
    "liquidity",         # 4. Volume and liquidity confirmation
    "execution",         # 5. Acceptable volatility and execution conditions
    "strategy_history",  # 6. Strategy historical validation (out-of-sample only)
)

# Equal weights by default. Override only through config, after review.
DEFAULT_WEIGHTS: Mapping[str, float] = {name: 1.0 for name in CONDITIONS}


@dataclass(frozen=True)
class ConvictionResult:
    """Output of score_conviction. Immutable so it can go straight into an audit record."""

    score: float                      # Final score in [0, 1]
    components: dict                  # Per-condition value actually used (missing -> 0.0)
    missing: tuple                    # Conditions that were None or NaN
    weights: dict                     # Normalised weights that were applied
    zeroed: tuple = field(default=())  # Conditions that were exactly 0.0 (hard fail)


def _validate_weights(weights: Mapping[str, float]) -> dict:
    """Return normalised weights (sum to 1) over exactly the six CONDITIONS."""
    unknown = set(weights) - set(CONDITIONS)
    if unknown:
        raise ValueError(f"unknown condition names in weights: {sorted(unknown)}")
    raw = {}
    for name in CONDITIONS:
        w = float(weights.get(name, 0.0))
        if not math.isfinite(w) or w < 0:
            raise ValueError(f"weight for {name!r} must be a finite non-negative number, got {w}")
        raw[name] = w
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("at least one condition weight must be positive")
    return {name: raw[name] / total for name in CONDITIONS}


def score_conviction(
    conditions: Mapping[str, Optional[float]],
    weights: Optional[Mapping[str, float]] = None,
) -> ConvictionResult:
    """Score one opportunity.

    Args:
        conditions: maps each of the six CONDITIONS to a value in [0, 1], or None
            if the value is unavailable. Extra keys are rejected.
        weights: optional non-negative weights. Normalised internally.
            Defaults to equal weights.

    Returns:
        ConvictionResult with the score and the audit detail.

    Raises:
        ValueError: on unknown keys, a value outside [0, 1], or invalid weights.
    """
    unknown = set(conditions) - set(CONDITIONS)
    if unknown:
        raise ValueError(f"unknown condition names: {sorted(unknown)}")

    norm_w = _validate_weights(weights if weights is not None else DEFAULT_WEIGHTS)

    components: dict = {}
    missing: list = []
    zeroed: list = []
    for name in CONDITIONS:
        raw = conditions.get(name)  # absent key behaves like None
        if raw is None or (isinstance(raw, float) and math.isnan(raw)):
            components[name] = 0.0
            missing.append(name)
            continue
        value = float(raw)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"condition {name!r} must be in [0, 1], got {value}")
        components[name] = value
        if value == 0.0:
            zeroed.append(name)

    # Weighted geometric mean. Any zero or missing component forces the score to 0
    # (fail closed). Without this check, log(0) would be reached for missing data.
    if zeroed or missing:
        score = 0.0
    else:
        log_sum = sum(norm_w[n] * math.log(components[n]) for n in CONDITIONS)
        score = math.exp(log_sum)

    # Guard against floating-point drift outside [0, 1].
    score = min(1.0, max(0.0, score))

    return ConvictionResult(
        score=score,
        components=components,
        missing=tuple(missing),
        weights=norm_w,
        zeroed=tuple(zeroed),
    )
