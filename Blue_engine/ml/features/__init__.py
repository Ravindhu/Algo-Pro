"""
ml/features — feature construction. This package is the leak barrier.

Everything upstream produces *data*. Everything downstream produces *models*.
This is where data becomes model input, and therefore where look-ahead either
enters the system or is stopped.

Modules
-------
point_in_time   the as-of join: attach only what was KNOWN at each timestamp
engineering     shift-disciplined price features (returns, vol, RSI, momentum)
labels          the targets — the only code allowed to read forward

THE CONTRACT IN ONE SENTENCE
----------------------------
**Features look backwards. Labels look forwards. Nothing looks sideways.**

"Sideways" is the subtle one: an aggregate over the whole sample (`zscore_latest`)
is neither backward nor forward, so it is not obviously wrong — but at prediction
time it cannot be computed, so the model sees a different feature distribution
live than in training. The leakage auditor's shift test catches it, and
`engineering.zscore_latest` is named to warn you before you get there.

HOW TO USE IT
-------------
    from blue_engine.ml.features import (
        engineering, labels, point_in_time,
    )

    # 1. Enrich prices with only what was known at each date.
    enriched = point_in_time.asof_join(
        prices, macro_rows, columns=["value"], prefix="cpi_")

    # 2. Build backward-looking features.
    feats = engineering.build_price_features(enriched)

    # 3. Build the target (the only forward-looking step).
    lab = labels.make_labels(enriched, kind="binary", horizon=5)

    # 4. Prove step 2 does not leak, BEFORE training.
    from blue_engine.ml.audit import audit_features
    audit_features(enriched,
                   build_features=engineering.build_price_features,
                   forward_returns=lab["forward_returns"])

Step 4 is not optional. A pipeline that skips it is a pipeline that will report
a beautiful backtest and then lose money.
"""

from __future__ import annotations

from . import engineering, labels, point_in_time

__all__ = ["engineering", "labels", "point_in_time"]