"""
ml/features/labels.py — the targets. The ONLY place allowed to look forward.

WHY LABELS GET THEIR OWN FILE
-----------------------------
A label is, by definition, future information: "did the price rise over the next
5 days?" Only the *label* may look forward; features may not. Keeping them in
separate modules means:

  * the leakage auditor can be told "labels live here, exclude them from the
    shift test" without a fragile column-name blocklist,
  * a reviewer can open one file and see every forward-looking expression in the
    whole system,
  * the last row of a dataset is always label-less, which is a *feature*: it is
    exactly the row you would predict on live.

THE FOUR THINGS THAT GO WRONG WITH LABELS (and what we do about each)
---------------------------------------------------------------------
1. **The label horizon leaks into the split.** If the label at `t` uses
   `t+5`, then a training set ending at `t` and a test set starting at `t+1`
   share five days of information. This is why `ml/evaluation/splits.py` purges
   and embargoes. Nothing here can fix it; the splitter must. The docstring
   repeats it so the two files are read together.

2. **The label is defined on the wrong quantity.** "Price went up" is not
   tradeable: costs, slippage and the entry price all matter. A 0.05% up-move is
   a loss after Indian equity costs (~0.1% round trip). So we provide a
   cost-aware label alongside the naive one, and the naive one is documented as
   *research only*.

3. **Class balance silently collapses.** A 1-day up/down label on NIFTY is close
   to 50/50, but a 5-day 2%-threshold label can be 80/20. A model that predicts
   the majority class gets 80% accuracy and zero edge. `label_balance()` exists so
   the trainer can log the real base rate and the evaluator can report skill
   *above that base rate* rather than raw accuracy.

4. **The target can simply be the wrong one.** Measured on real NIFTY daily
   bars (see `docs/ML_SKILL_DIAGNOSIS.md`), the SIGN of a 5-day index return is
   not predictable from standard price features — even in-sample, no feature
   beat the 54.25% majority class by more than 0.6 percentage points. That is a
   statement about the TARGET, not the model. So this module also offers:

     * ``kind="vol_ratio"`` — predict whether forward REALISED VOLATILITY is
       above its trailing norm. Volatility clustering is among the most
       robustly documented facts in finance (Engle 1982, Bollerslev 1986):
       high-volatility days cluster, which makes the level of vol materially
       more forecastable than its direction.
     * ``kind="triple_barrier_cost"`` — the López de Prado barriers with the
       round-trip cost INSIDE the barriers, so a "win" is only a win if it
       paid for the trade.

DEFINITION OF THE DEFAULT LABEL
-------------------------------
    label(t) = 1 if close(t+h) / close(t) - 1 >  threshold
             = 0 otherwise

with `h = 5` (one trading week) and `threshold = 0` by default. One week is
chosen over one day because a daily label is dominated by microstructure noise —
the signal-to-noise ratio for a 1-day horizon on an index is close to zero, and a
model trained on it mostly learns bid-ask bounce. One week is the shortest horizon
where macro/flow features have a plausible edge.

TRIPLE-BARRIER (why it is offered but not the default)
------------------------------------------------------
López de Prado's triple-barrier method labels by whichever comes first: a profit
target, a stop loss, or a time limit. It is closer to how a trade actually ends and
it produces a more balanced, more learnable target. It is offered here because it
is genuinely better for a *strategy* label — but it is NOT the default, because it
introduces two extra parameters (barrier widths) that must be tuned and can
themselves overfit. Start with the simple label, prove the pipeline, then switch.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from .. import logger

__all__ = [
    "forward_return", "binary_label", "triple_barrier", "vol_ratio_label",
    "realised_forward_vol", "trailing_vol", "label_balance",
    "make_labels", "LABEL_VERSION", "DEFAULT_COST_BPS", "LABEL_KINDS",
]

#: Every label kind this module can build. The trainer validates against this
#: rather than a hand-typed list, so adding a kind touches one place.
LABEL_KINDS = ("binary", "triple_barrier", "triple_barrier_cost", "vol_ratio")

#: Bump when a label definition changes. Stored in the artifact manifest: a model
#: trained against a different label definition is not comparable and must be
#: retrained, even if the column name is unchanged.
LABEL_VERSION = "label-v1"

#: Round-trip cost assumption for the cost-aware label, in basis points.
#: 10 bps covers Indian equity brokerage + STT + exchange charges + GST for a
#: delivery round trip at a typical discount broker. It is a *floor*: it ignores
#: slippage and impact, which is why the cost-aware label is stricter than the
#: naive one but still optimistic for size.
DEFAULT_COST_BPS = 10.0


def forward_return(closes: Sequence[Optional[float]], horizon: int = 5
                   ) -> List[Optional[float]]:
    """Return from `t` to `t+horizon`. The last `horizon` rows are None.

    This is the one place in the feature/label layer that reads ahead, and it is
    named `forward_*` so it is greppable in a review.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    n = len(closes)
    out: List[Optional[float]] = []
    for i in range(n):
        j = i + horizon
        if j >= n:
            # Deliberately None, not 0. The final rows are exactly the rows a
            # live model would predict on; they must never carry a fabricated
            # label that a trainer could accidentally fit.
            out.append(None)
            continue
        a, b = closes[i], closes[j]
        if a is None or b is None or a == 0:
            out.append(None)
        else:
            out.append(b / a - 1.0)
    return out


def binary_label(fwd_returns: Sequence[Optional[float]], *,
                 threshold: float = 0.0,
                 cost_bps: float = 0.0) -> List[Optional[int]]:
    """1 when the forward return beats `threshold` (plus costs), else 0.

    Args:
        fwd_returns: output of :func:`forward_return`.
        threshold: required return, as a fraction (0.01 = 1%).
        cost_bps: round-trip cost to subtract before comparing. Set this for a
            *tradeable* label. With the default 10 bps, a 0.05% up-move labels as
            0 — correctly, because it is a loss after costs.

    Returns:
        List of 0/1, with None where the forward return is unknown.
    """
    need = float(threshold) + float(cost_bps) / 10_000.0
    return [None if r is None else (1 if r > need else 0) for r in fwd_returns]


def triple_barrier(closes: Sequence[Optional[float]], *,
                   profit_pct: float = 0.02,
                   stop_pct: float = 0.01,
                   horizon: int = 10,
                   ) -> List[Optional[int]]:
    """Label by the FIRST barrier touched: profit, stop, or time limit.

    Returns 1 if the profit barrier is hit first, 0 if the stop is hit first, and
    the sign of the return at the horizon if neither is hit. None where there is
    not enough forward data.

    Why the asymmetric default (2% profit / 1% stop): a 2:1 reward-to-risk target
    is the standard swing-trade shape, and it produces a *balanced* label even
    when the underlying drifts, because the nearer stop is touched more often.
    Both parameters are configurable; tune them on training data only.

    Walk-forward note: the loop scans forward until a barrier is touched, so the
    label at `t` depends on up to `horizon` future bars. That is the label's job —
    but it means the purge/embargo in the splitter must be at least `horizon`
    bars. The splitter reads `horizon` from the run config for exactly this.
    """
    n = len(closes)
    out: List[Optional[int]] = []
    for i in range(n):
        entry = closes[i]
        if entry is None or entry <= 0:
            out.append(None)
            continue
        upper = entry * (1.0 + profit_pct)
        lower = entry * (1.0 - stop_pct)
        label: Optional[int] = None
        last: Optional[float] = None
        for j in range(i + 1, min(i + 1 + horizon, n)):
            px = closes[j]
            if px is None:
                continue
            last = px
            if px >= upper:
                label = 1
                break
            if px <= lower:
                label = 0
                break
        if label is None:
            if last is None:
                out.append(None)          # no forward data at all
            else:
                out.append(1 if last > entry else 0)
        else:
            out.append(label)
    return out


def trailing_vol(closes: Sequence[Optional[float]], window: int = 20, *,
                 shift: int = 0) -> List[Optional[float]]:
    """Realised vol over the TRAILING `window` bars, ending at `t - shift`.

    Daily log-return standard deviation, annualised by sqrt(252). Used as the
    *norm* a forward volatility label is compared against.

    Args:
        shift: 0 includes the current bar's return (correct when the label is a
            FORWARD volatility, because today's realised vol is knowable now).
            Use shift>=1 only if you also exclude today's return.
    """
    n = len(closes)
    rets: List[Optional[float]] = [None] * n
    for i in range(1, n):
        a, b = closes[i - 1], closes[i]
        if a is None or b is None or a <= 0 or b <= 0:
            continue
        rets[i] = math.log(b / a)

    out: List[Optional[float]] = [None] * n
    for i in range(n):
        end = i - shift + 1
        start = end - window
        if start < 1 or end > n:
            continue
        vals = [r for r in rets[start:end] if r is not None]
        if len(vals) < max(5, window // 2):
            continue
        mu = sum(vals) / len(vals)
        var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
        out[i] = math.sqrt(var) * math.sqrt(252.0)
    return out


def realised_forward_vol(closes: Sequence[Optional[float]], horizon: int = 5
                         ) -> List[Optional[float]]:
    """Realised vol over the FORWARD `horizon` bars [t+1 .. t+horizon].

    This is the quantity a volatility model predicts, and it is the one place
    in this module (alongside `forward_return`) that legitimately reads ahead.
    The last `horizon` rows are None — exactly the rows a live model would
    predict on.
    """
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        j = i + horizon
        if j >= n:
            continue
        vals: List[float] = []
        for k in range(i + 1, j + 1):
            a, b = closes[k - 1], closes[k]
            if a is None or b is None or a <= 0 or b <= 0:
                continue
            vals.append(math.log(b / a))
        if len(vals) < max(2, horizon // 2):
            continue
        mu = sum(vals) / len(vals)
        var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
        out[i] = math.sqrt(var) * math.sqrt(252.0)
    return out


def vol_ratio_label(closes: Sequence[Optional[float]], *,
                    horizon: int = 5,
                    norm_window: int = 20,
                    ratio: float = 1.0) -> List[Optional[int]]:
    """1 when FORWARD realised vol exceeds `ratio` x trailing realised vol.

    WHY VOLATILITY AND NOT DIRECTION
    --------------------------------
    Predicting the sign of an index return is close to the hardest available
    target: measured on real NIFTY daily bars, standard price features could
    not beat a 54.25% majority class (docs/ML_SKILL_DIAGNOSIS.md). Volatility
    is a different problem. Volatility clusters — today's vol is informative
    about tomorrow's — so a model has a genuine, well-documented signal to
    find (Engle 1982; Bollerslev 1986).

    The label is *relative* (forward vol vs its own trailing norm), not
    absolute. That matters: an absolute threshold would put every 2020 bar in
    one class and every 2017 bar in the other, and the model would simply
    learn the calendar.

    Args:
        ratio: 1.0 asks "is forward vol above its trailing norm?". Tune this
            on TRAIN data only — it is a threshold, and thresholds overfit.

    Returns:
        0/1 per row, None where either vol estimate is undefined (the warm-up
        and the label-less tail).
    """
    fwd = realised_forward_vol(closes, horizon)
    norm = trailing_vol(closes, norm_window)
    out: List[Optional[int]] = []
    for f, t in zip(fwd, norm):
        if f is None or t is None or t <= 0:
            out.append(None)
        else:
            out.append(1 if f > ratio * t else 0)
    return out


def triple_barrier_cost(closes: Sequence[Optional[float]], *,
                       profit_pct: float = 0.02,
                       stop_pct: float = 0.01,
                       horizon: int = 10,
                       cost_bps: float = DEFAULT_COST_BPS
                       ) -> List[Optional[int]]:
    """Triple barrier with the round-trip cost INSIDE the profit barrier.

    The plain :func:`triple_barrier` calls a 2% up-move a win. After a 10 bps
    round trip the realised gain is 1.9% — still a win, but the *breakeven*
    case is the one that matters: a move that just reaches the profit barrier
    can be a net loss. This variant raises the profit barrier by the cost, so
    a row labelled 1 is one that would have cleared the trade after charges.

    Example (2% profit target, 10 bps cost):
        plain           barrier at +2.00%   -> labels a +2.00% move as a win
        cost-aware      barrier at +2.10%   -> requires +2.10% to be a win

    The stop is left unchanged on purpose. Costs are symmetric, but a stop is
    a *risk* control: raising it would cut losses later than intended, which
    is the opposite of what a stop is for.
    """
    return triple_barrier(closes,
                          profit_pct=profit_pct + cost_bps / 10_000.0,
                          stop_pct=stop_pct, horizon=horizon)


def label_balance(labels: Sequence[Optional[int]]) -> Dict[str, Any]:
    """Class balance report. Logged by the trainer, shown by the evaluator.

    A model's accuracy is meaningless without this number. If the base rate is
    80% and the model scores 80%, the model has learned nothing — it predicts the
    majority class. The evaluator therefore always reports skill *relative* to
    this, and this function is what supplies the denominator.
    """
    known = [int(v) for v in labels if v is not None]
    n = len(known)
    if not n:
        return {"n": 0, "pos": 0, "neg": 0, "base_rate": None, "note": "no labels"}
    pos = sum(known)
    neg = n - pos
    rate = pos / n
    note = "balanced"
    if rate > 0.75 or rate < 0.25:
        note = ("IMBALANCED — a majority-class predictor would score "
                f"{max(rate, 1 - rate):.1%}. Report skill above this base rate, "
                f"never raw accuracy.")
    return {"n": n, "pos": pos, "neg": neg, "base_rate": round(rate, 4),
            "note": note}


def make_labels(rows: Sequence[Dict[str, Any]], *,
                kind: str = "binary",
                close_col: str = "close",
                horizon: int = 5,
                threshold: float = 0.0,
                cost_bps: float = DEFAULT_COST_BPS,
                profit_pct: float = 0.02,
                stop_pct: float = 0.01,
                norm_window: int = 20,
                vol_ratio: float = 1.0,
                ) -> Dict[str, Any]:
    """Build labels for a dataset. Returns the labels plus their provenance.

    The returned dict is what the trainer stores in the manifest, so a model can
    always be traced back to the exact label definition that produced it.

    Args:
        kind: one of :data:`LABEL_KINDS` — "binary" (default), "triple_barrier",
            "triple_barrier_cost" (barriers net of charges) or "vol_ratio"
            (forward volatility vs its trailing norm).
        horizon: forward bars. Must be >= the splitter's embargo (documented, not
            enforced here, because the splitter owns the split).

    Returns:
        ``{"labels": [...], "kind", "horizon", "threshold", "cost_bps",
           "balance": {...}, "label_version": str}``
    """
    closes = []
    for r in rows:
        try:
            v = r.get(close_col)
            closes.append(None if v is None or v == "" else float(v))
        except (TypeError, ValueError):
            closes.append(None)

    fwd = forward_return(closes, horizon)

    if kind == "triple_barrier":
        labels = triple_barrier(closes, profit_pct=profit_pct,
                                stop_pct=stop_pct, horizon=horizon)
    elif kind == "triple_barrier_cost":
        labels = triple_barrier_cost(closes, profit_pct=profit_pct,
                                     stop_pct=stop_pct, horizon=horizon,
                                     cost_bps=cost_bps)
    elif kind == "vol_ratio":
        labels = vol_ratio_label(closes, horizon=horizon,
                                 norm_window=norm_window, ratio=vol_ratio)
    elif kind == "binary":
        labels = binary_label(fwd, threshold=threshold, cost_bps=cost_bps)
    else:
        # Name the accepted values in the error: a typo in a config file is the
        # most likely cause, and the fix should be readable in the message.
        raise ValueError(f"unknown label kind {kind!r}; expected one of "
                         f"{list(LABEL_KINDS)}")

    bal = label_balance(labels)
    logger.info("labels: kind=%s horizon=%d n=%d base_rate=%s (%s)",
                kind, horizon, bal["n"], bal["base_rate"], bal["note"])

    return {
        "labels": labels,
        "kind": kind,
        "horizon": horizon,
        "threshold": threshold if kind == "binary" else None,
        "cost_bps": cost_bps if kind in ("binary", "triple_barrier_cost") else None,
        "profit_pct": (profit_pct
                       if kind in ("triple_barrier", "triple_barrier_cost")
                       else None),
        "stop_pct": (stop_pct
                     if kind in ("triple_barrier", "triple_barrier_cost")
                     else None),
        "norm_window": norm_window if kind == "vol_ratio" else None,
        "vol_ratio": vol_ratio if kind == "vol_ratio" else None,
        "forward_returns": fwd,
        # For a vol target the *magnitude* being predicted is vol, not return.
        # Recording it lets the evaluator compute a cost-adjusted edge that is
        # about vol forecating rather than a phantom price P&L.
        "forward_vol": (realised_forward_vol(closes, horizon)
                        if kind == "vol_ratio" else None),
        "balance": bal,
        "label_version": LABEL_VERSION,
    }