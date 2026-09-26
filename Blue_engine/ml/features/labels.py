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

THE THREE THINGS THAT GO WRONG WITH LABELS (and what we do about each)
----------------------------------------------------------------------
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

from typing import Any, Dict, List, Optional, Sequence

from .. import logger

__all__ = [
    "forward_return", "binary_label", "triple_barrier", "label_balance",
    "make_labels", "LABEL_VERSION",
]

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
                ) -> Dict[str, Any]:
    """Build labels for a dataset. Returns the labels plus their provenance.

    The returned dict is what the trainer stores in the manifest, so a model can
    always be traced back to the exact label definition that produced it.

    Args:
        kind: "binary" (default) or "triple_barrier".
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
    elif kind == "binary":
        labels = binary_label(fwd, threshold=threshold, cost_bps=cost_bps)
    else:
        raise ValueError(f"unknown label kind {kind!r}; "
                         f"expected 'binary' or 'triple_barrier'")

    bal = label_balance(labels)
    logger.info("labels: kind=%s horizon=%d n=%d base_rate=%s (%s)",
                kind, horizon, bal["n"], bal["base_rate"], bal["note"])

    return {
        "labels": labels,
        "kind": kind,
        "horizon": horizon,
        "threshold": threshold,
        "cost_bps": cost_bps if kind == "binary" else None,
        "profit_pct": profit_pct if kind == "triple_barrier" else None,
        "stop_pct": stop_pct if kind == "triple_barrier" else None,
        "forward_returns": fwd,
        "balance": bal,
        "label_version": LABEL_VERSION,
    }