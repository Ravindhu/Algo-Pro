"""
ml/evaluation/metrics.py — honest performance measurement.

THE PROBLEM WITH THE METRICS EVERYONE USES
------------------------------------------
A quant model's reported accuracy is almost always wrong, and wrong in the
flattering direction. Three specific mechanisms, each with a fix here:

1. **Raw accuracy is meaningless under class imbalance.**
   If 78% of days are up, a model that always says "up" scores 78%. So we always
   report `base_rate` next to `accuracy`, plus **skill** = accuracy − base rate.
   A model with skill 0.00 has learned nothing regardless of how good 0.78 looks.

2. **In-sample metrics are not performance.**
   A GradientBoosting model with enough trees fits any training set perfectly.
   The only numbers that mean anything come from walk-forward folds, which is why
   every function here takes a *list* of fold results and reports the distribution
   (mean, std, min) rather than a single figure. A mean of 0.62 with a std of 0.18
   is a very different proposition from 0.62 with std 0.02 — the first is luck.

3. **A high Sharpe from a search is not a high Sharpe.**
   Test 100 feature/model combinations and the best will look good by chance
   alone. `deflated_sharpe` (Bailey & López de Prado) corrects for exactly that:
   it takes the *number of trials* and shrinks the observed Sharpe toward zero
   accordingly. If you tried 100 variants and got Sharpe 1.5, the deflated number
   is the one to believe.

WHY NO numpy / sklearn
----------------------
Same house rule as `core/results.py`: the evaluation layer must run on a bare
machine. It also keeps every formula readable — you can check `auc` against the
Mann-Whitney definition by eye, which you cannot do with a library call. The
samples here are small (a few hundred rows per fold), so the pure-Python cost is
irrelevant.

WHAT TO LOOK AT, IN ORDER
-------------------------
    1. skill          > 0 across folds? (if not, stop — nothing else matters)
    2. fold std       small relative to skill? (stability)
    3. turnover/cost  does the edge survive Indian transaction costs?
    4. deflated Sharpe  does it survive the fact that you searched?
    5. calibration    are the probabilities usable for position sizing?
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from .. import logger

__all__ = [
    "accuracy", "base_rate", "skill", "auc", "precision_recall",
    "brier_score", "log_loss", "hit_rate", "sharpe",
    "max_drawdown", "deflated_sharpe", "fold_summary", "cost_adjusted_edge",
    "majority_bar",
]


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def accuracy(y_true: Sequence[Optional[int]],
             y_pred: Sequence[Optional[int]]) -> Optional[float]:
    """Fraction correct, over rows where both are known. None if none are."""
    pairs = [(int(a), int(b)) for a, b in zip(y_true, y_pred)
             if a is not None and b is not None]
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def base_rate(y_true: Sequence[Optional[int]]) -> Optional[float]:
    """Fraction of positives. The number `accuracy` must be compared against."""
    known = [int(v) for v in y_true if v is not None]
    if not known:
        return None
    return sum(known) / len(known)


def skill(y_true: Sequence[Optional[int]],
          y_pred: Sequence[Optional[int]]) -> Optional[float]:
    """Accuracy minus base rate of the *majority class*.

    Using the majority class (not the positive rate) is the stricter and more
    honest baseline: it is the score of the dumbest possible predictor, which is
    the actual bar to clear.

    Interpretation:
        <= 0.00   no edge. Do not trade it.
        0.00-0.02 marginal, and probably inside noise for a few hundred rows.
        0.02-0.05 a real but modest edge — typical of a usable daily model.
        > 0.10    treat with suspicion. Either a genuine strong signal or a leak;
                  run the leakage audit before believing it.
    """
    acc = accuracy(y_true, y_pred)
    known = [int(v) for v in y_true if v is not None]
    if acc is None or not known:
        return None
    maj = max(sum(known), len(known) - sum(known)) / len(known)
    return acc - maj


def auc(y_true: Sequence[Optional[int]],
        y_score: Sequence[Optional[float]]) -> Optional[float]:
    """Rank-based AUC (Mann-Whitney U), 0.5 = no discrimination.

    AUC is preferred over accuracy for a trading model because it is threshold-free
    and insensitive to class balance. A model with AUC 0.55 on daily bars has a
    real edge even if its 50%-threshold accuracy looks like a coin flip — the edge
    lives in the confidence, which is what position sizing consumes.

    Computed by counting concordant pairs directly, with ties counted as 0.5. That
    is O(n_pos * n_neg) but obviously correct, and n here is at most a few
    thousand. If a dataset ever reaches millions of rows, switch to the rank
    formulation — but not before, because the direct count is the one you can
    verify by hand.
    """
    pairs = [(int(a), float(b)) for a, b in zip(y_true, y_score)
             if a is not None and b is not None]
    pos = [s for y, s in pairs if y == 1]
    neg = [s for y, s in pairs if y == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for q in neg:
            if p > q:
                wins += 1.0
            elif p == q:
                wins += 0.5          # a tie is half a win
    return wins / (len(pos) * len(neg))


def precision_recall(y_true: Sequence[Optional[int]],
                     y_pred: Sequence[Optional[int]]
                     ) -> Dict[str, Optional[float]]:
    """Precision, recall and F1 for the positive class.

    Precision answers the trader's real question: "when the model says buy, how
    often is it right?" Recall answers "of all the real opportunities, how many
    did we take?" A strategy can be profitable with low recall and high precision
    (few, good trades) — which is usually preferable to the reverse.
    """
    tp = fp = fn = 0
    for a, b in zip(y_true, y_pred):
        if a is None or b is None:
            continue
        if int(a) == 1 and int(b) == 1:
            tp += 1
        elif int(a) == 0 and int(b) == 1:
            fp += 1
        elif int(a) == 1 and int(b) == 0:
            fn += 1
    prec = (tp / (tp + fp)) if (tp + fp) else None
    rec = (tp / (tp + fn)) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    return {"precision": prec, "recall": rec, "f1": f1,
            "tp": tp, "fp": fp, "fn": fn}


def brier_score(y_true: Sequence[Optional[int]],
                proba: Sequence[Optional[float]]) -> Optional[float]:
    """Mean squared error of probabilities. Lower is better; 0.25 = coin flip.

    This is the metric that matters if the probabilities drive *position size*
    rather than a binary decision. A model can rank perfectly (AUC 0.9) and still
    be badly calibrated (says 0.9 when it means 0.6), and using those numbers as
    sizing weights would systematically over-leverage.
    """
    pairs = [(int(a), float(b)) for a, b in zip(y_true, proba)
             if a is not None and b is not None]
    if not pairs:
        return None
    return sum((b - a) ** 2 for a, b in pairs) / len(pairs)


def log_loss(y_true: Sequence[Optional[int]],
             proba: Sequence[Optional[float]], *, eps: float = 1e-15
             ) -> Optional[float]:
    """Cross-entropy. Clipped to avoid log(0) producing an infinite score."""
    pairs = [(int(a), float(b)) for a, b in zip(y_true, proba)
             if a is not None and b is not None]
    if not pairs:
        return None
    total = 0.0
    for a, b in pairs:
        p = min(max(b, eps), 1.0 - eps)
        total += -(a * math.log(p) + (1 - a) * math.log(1.0 - p))
    return total / len(pairs)


# --------------------------------------------------------------------------- #
# strategy-level
# --------------------------------------------------------------------------- #


def hit_rate(returns: Sequence[Optional[float]]) -> Optional[float]:
    """Fraction of periods with a positive return (the naive win rate)."""
    known = [float(r) for r in returns if r is not None]
    if not known:
        return None
    return sum(1 for r in known if r > 0) / len(known)


def sharpe(returns: Sequence[Optional[float]], *,
           periods_per_year: float = 252.0,
           risk_free: float = 0.0) -> Optional[float]:
    """Annualised Sharpe ratio from a return series.

    Args:
        returns: per-period simple returns.
        periods_per_year: 252 for daily, 52 for weekly.
        risk_free: per-period risk-free rate, as a fraction. For India, the
            relevant anchor is the 91-day T-bill (~6.5-7%), which is ~0.00026
            per day. Leaving it at 0 slightly flatters every result; passing it
            is more honest. It is a parameter rather than a hardcode because the
            rate changes and a stale constant silently biases every report.
    """
    clean = [float(r) for r in returns if r is not None]
    n = len(clean)
    if n < 2:
        return None
    excess = [r - risk_free for r in clean]
    mu = sum(excess) / n
    var = sum((r - mu) ** 2 for r in excess) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (mu / sd) * math.sqrt(periods_per_year)


def max_drawdown(equity: Sequence[Optional[float]]) -> Optional[float]:
    """Largest peak-to-trough decline as a positive fraction (0.25 = -25%).

    Reported as a positive number because "drawdown 0.25" reads unambiguously,
    whereas "-0.25" invites the sign to be misread in a report.
    """
    clean = [float(e) for e in equity if e is not None]
    if len(clean) < 2:
        return None
    peak = clean[0]
    worst = 0.0
    for e in clean:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak
            if dd > worst:
                worst = dd
    return worst


def deflated_sharpe(observed_sharpe: float, *,
                    n_trials: int,
                    n_obs: int,
                    skew: float = 0.0,
                    kurtosis: float = 3.0,
                    trial_sharpe_var: Optional[float] = None) -> Dict[str, Any]:
    """Bailey & López de Prado's deflated Sharpe — correct for the search.

    THE PROBLEM: if you test 100 strategies and pick the best, its Sharpe is
    biased upward by the selection itself. The expected maximum Sharpe from 100
    random strategies is meaningfully above zero, so a "significant" 1.5 can be
    pure luck.

    THE FIX: raise the bar to the level a random search of the same size would be
    expected to reach, then report the probability the observed Sharpe is real.

    Args:
        observed_sharpe: the best Sharpe you found (per-period, NOT annualised —
            the maths assumes the same frequency as `n_obs`).
        n_trials: how many variants you evaluated. **Be honest here.** If you
            tried 20 feature sets, 5 models and 3 horizons, that is 300 trials,
            not 3. Under-reporting trials is the single most common way this
            correction gets defeated.
        n_obs: number of return observations.
        skew, kurtosis: of the return series. Defaults are the Gaussian values;
            real returns are negatively skewed with fat tails, which *widens* the
            confidence interval and lowers the deflated score. Pass the real
            values when you have them.
        trial_sharpe_var: variance of the per-period Sharpe ratios across the
            n_trials variants (Bailey & Lopez de Prado). The selection threshold
            scales with its square root. When None (legacy behaviour) the
            variance is taken as 1.0, which is far too large for realistic trial
            sets and makes the test stricter than it should be. The output records
            which assumption was used in ``trial_sharpe_var_used``.

    Returns:
        ``{"observed", "expected_max_sharpe", "deflated_sharpe", "prob_real",
           "n_trials", "verdict"}``

        `prob_real` is the probability the true Sharpe exceeds the selection
        threshold. Below ~0.95 the result is not established.

    Note on the normal approximation: the expected maximum uses the
    Gumbel/EVT approximation `E[max] ~ (1-g)*Z^-1(1-1/N) + g*Z^-1(1-1/(N*e))`
    with the Euler-Mascheroni constant g. It is accurate for N >= ~10 trials,
    which is every realistic case here.
    """
    if n_trials < 1 or n_obs < 2:
        return {"observed": observed_sharpe, "expected_max_sharpe": None,
                "deflated_sharpe": None, "prob_real": None,
                "n_trials": n_trials, "verdict": "insufficient input"}

    # Euler-Mascheroni constant, used by the EVT approximation below.
    gamma = 0.5772156649

    # Expected maximum of N independent standard normals.
    def _z_inv(p: float) -> float:
        """Inverse standard normal CDF (Acklam's rational approximation).

        Accurate to ~1e-9 over the useful range, and dependency-free — the same
        reason the rest of this module avoids scipy.
        """
        if p <= 0.0:
            return float("-inf")
        if p >= 1.0:
            return float("inf")
        a = [-3.969683028665376e+01, 2.209460984245205e+02,
             -2.759285104469687e+02, 1.383577518672690e+02,
             -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02,
             -1.556989798598866e+02, 6.680131188771972e+01,
             -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01,
             -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01,
             2.445134137142996e+00, 3.754408661907416e+00]
        p_low, p_high = 0.02425, 1 - 0.02425
        if p < p_low:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                    + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        if p > p_high:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                     + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r
                + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r
                                + b[4]) * r + 1)

    n = float(n_trials)
    v = 1.0 if trial_sharpe_var is None else float(trial_sharpe_var)
    if v < 0:
        raise ValueError("trial_sharpe_var must be >= 0")
    if n == 1.0:
        # With one strategy there is no multiple-testing selection premium.
        # The EVT approximation's first quantile is otherwise Z^-1(0) = -inf,
        # which contaminates every downstream DSR value with infinity/NaN.
        e_max = 0.0
    else:
        e_max = math.sqrt(v) * ((1 - gamma) * _z_inv(1 - 1.0 / n)
                                + gamma * _z_inv(1 - 1.0 / (n * math.e)))

    # Standard error of the Sharpe estimate, corrected for skew and kurtosis.
    # Fat tails make the estimate less certain, so the denominator grows.
    se = math.sqrt((1 - skew * observed_sharpe
                    + ((kurtosis - 1) / 4.0) * observed_sharpe ** 2)
                   / max(1.0, (n_obs - 1)))

    if se <= 0:
        return {"observed": observed_sharpe, "expected_max_sharpe": e_max,
                "deflated_sharpe": observed_sharpe, "prob_real": None,
                "n_trials": n_trials, "verdict": "degenerate standard error"}

    deflated = (observed_sharpe - e_max) / se

    # Probability the true Sharpe exceeds the selection threshold.
    prob = 0.5 * (1.0 + math.erf(deflated / math.sqrt(2.0)))

    if prob >= 0.95:
        verdict = "established — survives correction for the search"
    elif prob >= 0.80:
        verdict = "suggestive — needs more data or fewer trials"
    else:
        verdict = "NOT established — could be the best of a lucky search"

    return {
        "observed": round(observed_sharpe, 4),
        "expected_max_sharpe": round(e_max, 4),
        "deflated_sharpe": round(deflated, 4),
        "prob_real": round(prob, 4),
        "n_trials": n_trials,
        "n_obs": n_obs,
        "trial_sharpe_var_used": v,
        "verdict": verdict,
    }


def cost_adjusted_edge(gross_edge_bps: float, *,
                       turnover_per_period: float = 1.0,
                       cost_bps_per_turnover: float = 10.0) -> Dict[str, Any]:
    """Does the edge survive Indian transaction costs?

    A model with a 12 bps daily edge sounds tradeable until you realise a full
    round trip on delivery equity costs ~10 bps in charges alone (brokerage + STT
    + exchange + GST + stamp), before slippage and impact. If the strategy turns
    over once a day, the cost eats the entire edge.

    This is the check that kills most high-frequency ideas and it belongs next to
    the accuracy metrics, not in a separate report — because an edge that does not
    survive costs is not an edge.

    Args:
        gross_edge_bps: mean edge per period, in basis points.
        turnover_per_period: 1.0 = fully replace the book each period.
        cost_bps_per_turnover: round-trip cost per unit turnover, in bps.

    Returns:
        ``{"gross_bps", "cost_bps", "net_bps", "survives", "verdict"}``
    """
    cost = abs(turnover_per_period) * cost_bps_per_turnover
    net = gross_edge_bps - cost
    return {
        "gross_bps": round(gross_edge_bps, 2),
        "cost_bps": round(cost, 2),
        "net_bps": round(net, 2),
        "survives": net > 0,
        "verdict": ("edge survives costs" if net > 0
                    else "edge does NOT survive costs — do not trade this"),
    }


# --------------------------------------------------------------------------- #
# the majority-class bar
# --------------------------------------------------------------------------- #


#: Accuracy a model must EXCEED for `skill` to be positive, given the folds.
#: Exposed as a named constant because it is the single most misunderstood
#: number in the report: `skill` is measured against the majority CLASS, so in
#: a market that rose 54% of the time the bar is 54%, not 50%.
def majority_bar(base_rate: Optional[float]) -> Optional[float]:
    """The accuracy an always-predict-the-majority model achieves.

    This is the bar `skill` is measured against. In a trending market it is
    well above 0.5, which is why a model can look "about right" at 50%
    accuracy and still score negative skill.

    Worked example (the real NIFTY diagnosis, h=5):
        base_rate 0.5425 -> bar 0.5425. A model at 0.4732 scores -0.0693.
        The model was not broken; it was simply worse than always-long.
    """
    if base_rate is None:
        return None
    b = float(base_rate)
    return max(b, 1.0 - b)


def _bar_verdict(out: Dict[str, Any]) -> Optional[str]:
    """Explain the skill result against the majority-class bar.

    Returns a sentence for the report, or None when there is not enough
    information (e.g. no base_rate recorded, which happens for a regression
    target that has no classes).

    WHY THIS EXISTS: a bare `skill = -0.0693` reads like a bug. The same number
    with its bar attached reads as the finding it actually is: *the model did
    worse than simply always predicting the majority class*. That single
    sentence is the difference between an operator hunting a phantom bug and
    one moving on to a better target.
    """
    sk = out.get("skill")
    acc = out.get("accuracy")
    if not isinstance(sk, dict) or not isinstance(acc, dict):
        return None
    mean_acc = acc.get("mean")
    if mean_acc is None:
        return None

    # Recover the bar from the recorded skill: skill = acc - bar, so
    # bar = acc - skill. This avoids needing base_rate threaded through every
    # fold record, and it stays correct if a caller supplies skill directly.
    mean_skill = sk.get("mean")
    if mean_skill is None:
        return None
    bar = mean_acc - mean_skill

    if mean_skill <= 0:
        return (f"BELOW THE MAJORITY-CLASS BAR — accuracy {mean_acc:.4f} vs a "
                f"bar of {bar:.4f}. The model did WORSE than always predicting "
                f"the majority class (skill {mean_skill:+.4f}). This is not "
                f"necessarily a bug: check the feature screen before hunting one.")
    return (f"ABOVE THE MAJORITY-CLASS BAR — accuracy {mean_acc:.4f} vs a bar of "
            f"{bar:.4f} (skill {mean_skill:+.4f}).")


# --------------------------------------------------------------------------- #
# fold aggregation
# --------------------------------------------------------------------------- #


def fold_summary(fold_metrics: Sequence[Dict[str, Any]],
                 *, keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Aggregate per-fold metrics into mean / std / min / max.

    Why the distribution and not just the mean: a model with skill 0.05 ± 0.01 is
    a different animal from skill 0.05 ± 0.15. The second is a coin flip that
    averaged out well, and trading it is how accounts get blown. The std and the
    min are the risk information; the mean is only the centre.

    The returned dict always carries a `verdict` when `skill` is present, and
    that verdict names the majority-class bar explicitly. See
    :func:`_bar_verdict` for why that matters more than it looks.
    """
    if not fold_metrics:
        return {"n_folds": 0, "verdict": "NO FOLDS (nothing to aggregate)"}

    keys = list(keys) if keys else sorted({
        k for m in fold_metrics for k, v in m.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    })

    out: Dict[str, Any] = {"n_folds": len(fold_metrics)}
    for k in keys:
        vals = [float(m[k]) for m in fold_metrics
                if isinstance(m.get(k), (int, float))
                and not isinstance(m.get(k), bool)]
        if not vals:
            continue
        mu = sum(vals) / len(vals)
        if len(vals) > 1:
            sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))
        else:
            sd = 0.0
        out[k] = {"mean": round(mu, 4), "std": round(sd, 4),
                  "min": round(min(vals), 4), "max": round(max(vals), 4),
                  "n": len(vals)}

    # The bar first (it is what every other verdict is read against), then the
    # edge, then the dispersion. Order matters: it is the order the questions
    # actually get asked.
    if isinstance(out.get("skill"), dict):
        mu, sd = out["skill"]["mean"], out["skill"]["std"]
        acc = out.get("accuracy") or {}
        mean_acc = acc.get("mean")
        bar = majority_bar(None if mean_acc is None else mean_acc - mu)

        if mu <= 0:
            out["verdict"] = (
                f"NO EDGE — mean skill {mu:+.4f} <= 0. "
                + (f"Majority-class bar {(bar or 0):.4f}; the model is worse "
                   f"than always predicting the majority class." if bar else ""))
        elif sd > abs(mu):
            out["verdict"] = (f"UNSTABLE — skill {mu:.4f} +/- {sd:.4f}: the std "
                              f"exceeds the mean, so the edge is not consistent "
                              f"across folds")
        else:
            out["verdict"] = (f"CONSISTENT — skill {mu:.4f} +/- {sd:.4f}"
                              + (f", clearing a majority-class bar of "
                                 f"{bar:.4f}" if bar else ""))

        bar_note = _bar_verdict(out)
        if bar_note:
            out["bar_check"] = bar_note
    return out