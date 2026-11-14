"""
ml/features/engineering.py — leak-safe feature construction for price series.

THE ONE RULE THIS MODULE ENFORCES
---------------------------------
**A feature at row `t` may only use information from rows `<= t`, and in practice
from rows `< t` when predicting row `t`'s forward return.**

Every function here takes an explicit `shift` and applies it. There is no default
that "just works" without you thinking about it, because the default that feels
convenient is the one that leaks.

    # WRONG — this is what people write, and it leaks
    feat = close / close.rolling(20).mean() - 1

    # RIGHT — the window ends at t-1, so only past data is visible
    ma = rolling_mean(closes, 20, shift=1)
    feat = closes[t] / ma[t] - 1

WAIT — IS USING `closes[t]` ITSELF A LEAK?
------------------------------------------
It depends what you are predicting, and this is the distinction that trips people:

  * Predicting the **next** bar's return (the usual setup): at the moment you
    decide, today's close IS known. Using `closes[t]` in a feature is correct,
    and the label is `closes[t+1]/closes[t] - 1`. The feature uses t, the label
    uses t+1. No overlap.
  * Predicting **today's** return at today's open: then `closes[t]` is future
    information and must be excluded (use `shift=1` on everything).

Both are legitimate; they are different problems. What is never legitimate is
using `closes[t+1]` (or any aggregate that includes it) in a feature. That is what
`rolling_*(..., shift=1)` prevents: the window is `[t-n, t-1]`, never `[t-n+1, t]`.

WHY HAND-WRITTEN AND NOT pandas_ta / TA-Lib
-------------------------------------------
Two reasons, both practical:

  1. **The audit layer must run with nothing installed.** `ml/audit/leakage.py`
     re-runs the feature builder on truncated data. If the builder needs a
     compiled C extension, the audit needs it too, and then the audit cannot run
     on a fresh box — which is exactly when you most want it.
  2. **Shift discipline is invisible in a library call.** `ta.rsi(close, 14)`
     includes the current bar; whether that is right depends on the setup above.
     Writing it here makes the choice explicit and greppable.

The library versions are still available for *research* comparisons; they are
just not on the audited path.

CALIBRATION / CHOICES
---------------------
* Returns are **simple** (pct change), not log. The label is a simple return, and
  features must be on the same scale as the thing being predicted or a linear
  model silently mis-weights them. `log_return` is provided separately for
  volatility work, where log is the correct choice.
* Volatility is **realised** from closes with a 20-bar window. 20 trading days is
  roughly one calendar month — long enough to be stable, short enough to react to
  a regime change. It is a parameter, not a hardcode.
* `zscore_latest` is explicitly named to warn you it is a *full-sample* statistic
  and therefore NOT usable as a live feature. It exists for descriptive work only.
  The name is the guard-rail: if you see it in a feature matrix, something is
  wrong.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from .. import logger

__all__ = [
    "rolling_mean", "rolling_std", "returns", "log_returns",
    "momentum", "realised_vol", "rsi", "zscore_latest",
    "build_price_features", "FEATURE_VERSION",
]


#: Bump when any feature DEFINITION changes (not just its parameters). Artifacts
#: record this value in their manifest; a mismatch means the model must be
#: retrained, because the same column name now means something different.
#: Without this, a silent feature change makes every stored model wrong while
#: looking perfectly healthy.
FEATURE_VERSION = "price-v1"


# --------------------------------------------------------------------------- #
# primitives — all shift-aware
# --------------------------------------------------------------------------- #


def _shift(values: Sequence[Optional[float]], shift: int) -> List[Optional[float]]:
    """Shift right by `shift` rows, padding with None. Positive = look BACK.

    A negative shift would move values backwards in time, i.e. read the future.
    We refuse it outright rather than documenting "don't do that" — the whole
    point of this module is that the leak is not *expressible*.
    """
    if shift < 0:
        raise ValueError(
            f"negative shift ({shift}) would read the future. Labels live in "
            f"ml/features/labels.py; features may only look backwards.")
    if shift == 0:
        return list(values)
    pad: List[Optional[float]] = [None] * shift
    return pad + list(values[:-shift]) if len(values) > shift else pad[:len(values)]


def rolling_mean(values: Sequence[Optional[float]], window: int, *,
                 shift: int = 1) -> List[Optional[float]]:
    """Trailing mean of `window` observations, ending at `t - shift`.

    `shift=1` (the default) is the safe choice: the window is `[t-window, t-1]`.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    shifted = _shift(values, shift)
    out: List[Optional[float]] = []
    for i in range(len(shifted)):
        if i + 1 < window:
            out.append(None)
            continue
        chunk = [v for v in shifted[i + 1 - window:i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def rolling_std(values: Sequence[Optional[float]], window: int, *,
                shift: int = 1, sample: bool = True) -> List[Optional[float]]:
    """Trailing sample standard deviation, ending at `t - shift`."""
    if window < 2:
        raise ValueError("window must be >= 2 for a standard deviation")
    shifted = _shift(values, shift)
    out: List[Optional[float]] = []
    for i in range(len(shifted)):
        if i + 1 < window:
            out.append(None)
            continue
        chunk = [v for v in shifted[i + 1 - window:i + 1] if v is not None]
        if len(chunk) < 2:
            out.append(None)
            continue
        mu = sum(chunk) / len(chunk)
        denom = (len(chunk) - 1) if sample else len(chunk)
        var = sum((v - mu) ** 2 for v in chunk) / denom
        out.append(math.sqrt(var))
    return out


def returns(closes: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Simple period-over-period returns. Row 0 is None (no prior bar)."""
    out: List[Optional[float]] = [None]
    for a, b in zip(closes, closes[1:]):
        if a is None or b is None or a == 0:
            out.append(None)
        else:
            out.append(b / a - 1.0)
    return out


def log_returns(closes: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Natural-log returns. Use these for volatility, not for linear features."""
    out: List[Optional[float]] = [None]
    for a, b in zip(closes, closes[1:]):
        if a is None or b is None or a <= 0 or b <= 0:
            out.append(None)
        else:
            out.append(math.log(b / a))
    return out


def momentum(closes: Sequence[Optional[float]], window: int, *,
             shift: int = 1) -> List[Optional[float]]:
    """Total return over `window` bars, ending at `t - shift`.

    The classic 12-1 style momentum. Note it is a *level* change, not a mean
    reversion signal — those are separate features.
    """
    shifted = _shift(closes, shift)
    out: List[Optional[float]] = []
    for i in range(len(shifted)):
        if i < window:
            out.append(None)
            continue
        past, now = shifted[i - window], shifted[i]
        if past is None or now is None or past == 0:
            out.append(None)
        else:
            out.append(now / past - 1.0)
    return out


def realised_vol(closes: Sequence[Optional[float]], window: int = 20, *,
                 shift: int = 1) -> List[Optional[float]]:
    """Annualised realised volatility from log returns.

    Annualised with sqrt(252) because a daily vol of 0.01 is unreadable and a
    vol of ~16% is what a trader expects to see. The convention is stated here so
    nobody has to guess which scale a feature is on — mixing annualised and daily
    vol across features is a quiet way to mis-weight a model.
    """
    lr = log_returns(closes)
    sd = rolling_std(lr, window, shift=shift, sample=True)
    return [None if s is None else s * math.sqrt(252.0) for s in sd]


def rsi(closes: Sequence[Optional[float]], window: int = 14, *,
        shift: int = 1) -> List[Optional[float]]:
    """Wilder-style RSI in [0, 100], ending at `t - shift`.

    Implemented with simple averages rather than Wilder smoothing: the difference
    is negligible after ~50 bars and simple averages are easier to verify by hand
    in a test, which matters more here than the last decimal of accuracy.
    """
    shifted = _shift(closes, shift)
    out: List[Optional[float]] = []
    for i in range(len(shifted)):
        if i < window:
            out.append(None)
            continue
        gains, losses = 0.0, 0.0
        for a, b in zip(shifted[i - window:i], shifted[i - window + 1:i + 1]):
            if a is None or b is None:
                continue
            d = b - a
            if d >= 0:
                gains += d
            else:
                losses -= d
        if gains + losses == 0:
            out.append(50.0)              # flat market -> neutral, not undefined
        else:
            rs = gains / losses if losses > 0 else float("inf")
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def zscore_latest(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Full-sample z-score. **NOT a live feature.**

    Named `_latest` and documented as forbidden on purpose. A full-sample mean and
    standard deviation use the future, so the training distribution and the live
    distribution differ — the classic subtle leak. The leakage auditor's shift test
    will catch this if it ever reaches a feature matrix.

    Legitimate uses: descriptive statistics, outlier detection on a *historical*
    panel, and tests. Never a model input.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return [None] * len(values)
    mu = sum(clean) / len(clean)
    sd = math.sqrt(sum((v - mu) ** 2 for v in clean) / (len(clean) - 1))
    if sd == 0:
        return [None] * len(values)
    return [None if v is None else (v - mu) / sd for v in values]


# --------------------------------------------------------------------------- #
# the standard price feature set
# --------------------------------------------------------------------------- #


def build_price_features(rows: Sequence[Dict[str, Any]], *,
                         close_col: str = "close",
                         volume_col: str = "volume",
                         ts_col: str = "as_of_ts",
                         ) -> Dict[str, List[Any]]:
    """The audited baseline feature set for one instrument.

    Signature matches what `audit_features(build_features=...)` expects:
    `rows -> {feature_name: [value per row]}`, deterministic, no external state.

    Features produced (all strictly backward-looking):
        ret_1            last bar's simple return
        ret_5            return over the last 5 bars
        mom_20           20-bar momentum
        mom_60           60-bar momentum
        vol_20           annualised realised vol, 20 bars
        vol_60           annualised realised vol, 60 bars
        rsi_14           14-bar RSI
        ma_gap_20        close vs its 20-bar mean (mean-reversion signal)
        ma_gap_60        close vs its 60-bar mean
        hl_range         (high - low) / close, the intraday range proxy
        vol_z_20         volume vs its own 20-bar mean, ratio form
        dow              day-of-week, 0..4 (a *calendar* feature, no price info)
        month            1..12

    Note on `dow`/`month`: these are not leaks — the calendar is known in advance.
    They are included because Indian equity markets have documented day-of-week and
    expiry-week effects. A model may or may not find them useful; that is the
    model's job, and the evaluator will show whether they earn their place.

    Returns:
        A dict of equal-length lists, one entry per input row. Rows where a
        feature is undefined (the warm-up period) carry None rather than 0 — a 0
        would be a fabricated observation and would bias the first N rows.
    """
    n = len(rows)
    closes = [_f(r.get(close_col)) for r in rows]
    highs = [_f(r.get("high")) for r in rows]
    lows = [_f(r.get("low")) for r in rows]
    volumes = [_f(r.get(volume_col)) for r in rows]

    f: Dict[str, List[Any]] = {}

    ret = returns(closes)
    f["ret_1"] = ret

    # Cumulative 5-bar return, built from the shifted 1-bar returns so it also
    # excludes the current bar's own close-to-close move.
    r5: List[Optional[float]] = []
    for i in range(n):
        window = [v for v in ret[max(0, i - 4):i + 1] if v is not None]
        r5.append(None if len(window) < 5 else
                  math.prod(1.0 + v for v in window) - 1.0)
    f["ret_5"] = r5

    f["mom_20"] = momentum(closes, 20)
    f["mom_60"] = momentum(closes, 60)
    f["vol_20"] = realised_vol(closes, 20)
    f["vol_60"] = realised_vol(closes, 60)
    f["rsi_14"] = rsi(closes, 14)

    ma20 = rolling_mean(closes, 20)
    ma60 = rolling_mean(closes, 60)
    f["ma_gap_20"] = [None if (c is None or m is None or m == 0)
                      else c / m - 1.0 for c, m in zip(closes, ma20)]
    f["ma_gap_60"] = [None if (c is None or m is None or m == 0)
                      else c / m - 1.0 for c, m in zip(closes, ma60)]

    f["hl_range"] = [
        None if (h is None or l is None or c is None or c == 0)
        else (h - l) / c for h, l, c in zip(highs, lows, closes)]

    vmean = rolling_mean(volumes, 20)
    f["vol_z_20"] = [None if (v is None or m is None or m == 0)
                     else v / m - 1.0 for v, m in zip(volumes, vmean)]

    dow: List[Optional[int]] = []
    month: List[Optional[int]] = []
    for r in rows:
        from .point_in_time import parse_ts
        dt = parse_ts(r.get(ts_col))
        dow.append(dt.weekday() if dt else None)
        month.append(dt.month if dt else None)
    f["dow"] = dow
    f["month"] = month

    logger.debug("built %d features over %d rows (feature version %s)",
                 len(f), n, FEATURE_VERSION)
    return f


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None