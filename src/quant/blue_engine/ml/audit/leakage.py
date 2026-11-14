"""
ml/audit/leakage.py — the look-ahead tripwire.

THE PROBLEM THIS SOLVES, IN ONE PARAGRAPH
-----------------------------------------
Look-ahead bias is the only bug in quant research that makes your results
*better*. Every other bug makes the backtest worse, so you notice. A leak makes
the equity curve smooth, the Sharpe high and the hit-rate beautiful — and then
live trading loses money, because the model was never actually predicting, it was
remembering. It raises no exception, logs no warning, and survives code review
because the line that causes it (`close.pct_change()`) looks completely normal.

So we do not rely on discipline. We test for it mechanically, on every run.

THE THREE CHECKS
----------------
1. **as_of_ts presence and monotonicity** (`check_timestamps`)
   Every row must carry `as_of_ts`. Series must be sorted oldest-first with no
   duplicates. A duplicate timestamp means two different "truths" for one moment,
   which silently corrupts every lag/shift in the feature layer.

2. **The shift test** (`check_shift_alignment`) — the important one
   Build features twice: once on the real data, once on data with the FUTURE
   truncated (everything after the split point replaced by the past). If a
   feature's value at time t changes when the future is removed, that feature is
   reading the future. This is a *behavioural* test — it catches leaks regardless
   of how they were written, which is exactly why it is better than grepping for
   `.shift(-1)`.

   Worked example of what it catches that a code review would not:

       # Looks harmless. Passes review. Leaks 3 days of future information.
       vol = df['close'].rolling(20).std()          # includes today's close
       feat['vol_z'] = (vol - vol.mean()) / vol.std()   # mean over the WHOLE
                                                       # series, incl. the future

   The second line is the classic killer: a full-sample standardisation. At
   training time it uses the entire history; at prediction time it can only use
   the past. The distributions differ, so the model is fed a different-looking
   feature live than it saw in training.

3. **Correlation with the future** (`check_future_correlation`)
   A cheap, blunt screen: does any feature correlate suspiciously strongly with a
   *forward* return? Real predictors sit in a low band (typically |rho| < 0.15 for
   daily bars). A feature at |rho| > 0.5 on daily data is almost never a genuine
   signal — it is a leak. This check does not prove a leak exists (correlation is
   not causation, and a strong feature is possible), so it reports SUSPECT and
   asks for a human look rather than failing the run outright.

HOW THIS CONNECTS TO THE REST OF THE SYSTEM
-------------------------------------------
`ml/training/*` calls :func:`audit_features` before fitting. A FATAL finding
raises `LeakageError`, which the CLI reports and the dashboard shows in red. The
run produces no artifact — because an artifact trained on leaked features is worse
than no artifact: it will be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import logger
from ..errors import LeakageError
from .degradation import Severity

__all__ = [
    "LeakFinding", "check_timestamps", "check_shift_alignment",
    "check_future_correlation", "audit_features",
]


#: Above this |correlation| with a forward return, we flag SUSPECT rather than
#: silently trusting it. 0.5 is deliberately generous: it will not fire on a
#: genuinely strong signal (rare on daily bars) but will fire on the classic
#: leak, which typically shows |rho| > 0.9.
SUSPECT_CORR = 0.5


@dataclass
class LeakFinding:
    """One suspected or proven leak. Rendered on the dashboard verbatim."""

    check: str
    severity: Severity
    subject: str                  # feature/column name, or "<series>"
    detail: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"check": self.check, "severity": self.severity.value,
                "subject": self.subject, "detail": self.detail,
                "evidence": self.evidence}


# --------------------------------------------------------------------------- #
# 1. timestamps
# --------------------------------------------------------------------------- #


def check_timestamps(rows: Sequence[Dict[str, Any]], *,
                     ts_col: str = "as_of_ts",
                     require_sorted: bool = True) -> List[LeakFinding]:
    """Validate the knowledge-time column. Pure; never raises.

    Checks, each with a *specific* reason string so the dashboard can explain the
    consequence rather than just the fact:

      * column present on every row   -> without it, no lag/shift is meaningful
      * no empty values               -> an empty stamp becomes NaN in pandas and
                                         silently sorts to the front of the frame
      * sorted oldest-first           -> a shuffled frame makes `.shift(1)` mean
                                         "one random row", not "one day ago"
      * no duplicate timestamps       -> two rows for one instant makes every
                                         rolling window ambiguous
    """
    out: List[LeakFinding] = []

    if not rows:
        out.append(LeakFinding(
            "timestamps", Severity.FATAL, "<dataset>",
            "dataset is empty — nothing to validate, and nothing to train on."))
        return out

    missing = [i for i, r in enumerate(rows) if ts_col not in r]
    if missing:
        out.append(LeakFinding(
            "timestamps", Severity.FATAL, ts_col,
            f"{len(missing)} of {len(rows)} rows have no '{ts_col}'. Without a "
            f"knowledge timestamp a lag/shift is undefined and every feature "
            f"may be reading the future. Add it at the source.",
            {"first_missing_index": missing[0]}))

    empties = [i for i, r in enumerate(rows)
               if str(r.get(ts_col) or "").strip() == ""]
    if empties:
        out.append(LeakFinding(
            "timestamps", Severity.FATAL, ts_col,
            f"{len(empties)} row(s) have an empty '{ts_col}'. An empty stamp "
            f"becomes NaN and sorts unpredictably, which silently reorders the "
            f"series.", {"first_empty_index": empties[0]}))

    values = [str(r.get(ts_col) or "") for r in rows]
    if require_sorted and any(a > b for a, b in zip(values, values[1:])):
        # Report the first inversion so the message is actionable.
        idx = next(i for i in range(len(values) - 1) if values[i] > values[i + 1])
        out.append(LeakFinding(
            "timestamps", Severity.FATAL, ts_col,
            f"series is not sorted oldest-first (first inversion at index "
            f"{idx}: {values[idx]!r} then {values[idx + 1]!r}). `.shift(1)` on an "
            f"unsorted frame means 'one arbitrary row', not 'one day ago'.",
            {"index": idx}))

    dupes = len(values) - len(set(values))
    if dupes:
        out.append(LeakFinding(
            "timestamps", Severity.DEGRADED, ts_col,
            f"{dupes} duplicate timestamp(s). Two rows for one instant makes "
            f"every rolling window ambiguous and can double-count a bar.",
            {"duplicates": dupes}))

    return out


# --------------------------------------------------------------------------- #
# 2. the shift test — the behavioural check
# --------------------------------------------------------------------------- #


def check_shift_alignment(
    build_features: Callable[[Sequence[Dict[str, Any]]], Dict[str, List[Any]]],
    rows: Sequence[Dict[str, Any]],
    *,
    split_index: Optional[int] = None,
    ts_col: str = "as_of_ts",
    tolerance: float = 1e-9,
) -> List[LeakFinding]:
    """Detect leaks behaviourally: remove the future, see if the past changes.

    HOW IT WORKS
    ------------
    Let `build_features(rows) -> {feature_name: [value per row]}`.

      1. Compute features on the FULL dataset          -> `full`
      2. Truncate the dataset at `split_index` and
         recompute                                       -> `trunc`
      3. Compare `full` and `trunc` on rows
         `[0, split_index)`. They must be IDENTICAL.

    Why identical: rows before the split point cannot possibly depend on data
    after it. If a value *does* change, that feature consumed future information.

    Why this beats a code review: it is independent of implementation. It catches
    `rolling(...).mean()` that forgot `.shift(1)`, full-sample z-scores,
    `train_test_split` leakage, a stray `df['target']` in the feature matrix, and
    a target that got merged one row off — without knowing any of them by name.

    Args:
        build_features: pure function `rows -> {name: values}`. Must be
            deterministic and depend only on the rows passed in (no module state,
            no clock, no RNG without a fixed seed).
        rows: the dataset, sorted oldest-first.
        split_index: row index to cut at. Default = 70% of the series, which
            leaves a meaningful "past" and "future" while being long enough for
            rolling windows to be populated in the truncated run.
        tolerance: absolute float tolerance. Not exactly 0 because floating-point
            summation order can differ; a genuine leak is orders of magnitude
            larger than this, so nothing real hides under it.

    Returns:
        A list of findings. Empty list == no leak detected on the sampled rows.
    """
    out: List[LeakFinding] = []
    n = len(rows)
    if n < 20:
        out.append(LeakFinding(
            "shift", Severity.DEGRADED, "<dataset>",
            f"only {n} rows — too short for a meaningful shift test. "
            f"At least 20 are needed for a rolling window to be populated."))
        return out

    cut = int(split_index if split_index is not None else n * 0.7)
    cut = max(1, min(cut, n - 1))

    try:
        full = build_features(rows)
    except Exception as exc:  # noqa: BLE001 - a broken builder is its own finding
        out.append(LeakFinding(
            "shift", Severity.FATAL, "<builder>",
            f"feature builder raised on the full dataset: {type(exc).__name__}: "
            f"{exc}. Cannot audit what will not run."))
        return out

    try:
        trunc = build_features(rows[:cut])
    except Exception as exc:  # noqa: BLE001
        out.append(LeakFinding(
            "shift", Severity.FATAL, "<builder>",
            f"feature builder raised on the truncated dataset: "
            f"{type(exc).__name__}: {exc}. This usually means the builder "
            f"depends on something outside `rows` (module state, a file, a "
            f"clock) — which is itself a reproducibility leak."))
        return out

    if not isinstance(full, dict):
        out.append(LeakFinding(
            "shift", Severity.FATAL, "<builder>",
            "build_features must return {feature_name: [values]}; got "
            f"{type(full).__name__}."))
        return out

    for name, full_vals in full.items():
        if name not in trunc:
            # A feature that only exists when the future is present is itself a
            # leak signal (e.g. built from a forward-looking merge).
            out.append(LeakFinding(
                "shift", Severity.FATAL, name,
                f"feature '{name}' exists on the full dataset but not on the "
                f"truncated one — it is derived from data after the split.",
                {"full_len": len(full_vals or [])}))
            continue

        trunc_vals = trunc[name]
        mismatch_at: Optional[int] = None
        worst = 0.0
        for i in range(min(cut, len(full_vals or []), len(trunc_vals or []))):
            a, b = full_vals[i], trunc_vals[i]
            # None/NaN equality: both missing is fine, one missing is not.
            if a is None and b is None:
                continue
            if (a is None) != (b is None):
                mismatch_at, worst = i, float("inf")
                break
            try:
                d = abs(float(a) - float(b))
            except (TypeError, ValueError):
                if a != b:
                    mismatch_at, worst = i, float("inf")
                    break
                continue
            if d > worst:
                worst = d
            if d > tolerance and mismatch_at is None:
                mismatch_at = i

        if mismatch_at is not None:
            out.append(LeakFinding(
                "shift", Severity.FATAL, name,
                f"LEAK: feature '{name}' changes at index {mismatch_at} "
                f"(row {rows[mismatch_at].get(ts_col)}) when the future is "
                f"removed (delta={worst}). A value that depends on data after "
                f"the split is reading the future and will not exist in live "
                f"trading. Usual causes: a rolling window without .shift(1), a "
                f"full-sample mean/std normalisation, or a forward-looking merge.",
                {"index": mismatch_at, "delta": worst,
                 "ts": rows[mismatch_at].get(ts_col)}))

    return out


# --------------------------------------------------------------------------- #
# 3. correlation screen
# --------------------------------------------------------------------------- #


def check_future_correlation(
    features: Dict[str, Sequence[Optional[float]]],
    forward_returns: Sequence[Optional[float]],
    *,
    threshold: float = SUSPECT_CORR,
    min_obs: int = 30,
) -> List[LeakFinding]:
    """Flag features implausibly correlated with a forward return.

    This is a *screen*, not a proof. It reports DEGRADED (SUSPECT), never FATAL,
    for two honest reasons:

      1. A genuinely strong predictor can exist (a scheduled event, a limit-up
         day). Failing the run on a real signal would be its own kind of error.
      2. Correlation is measured over the whole sample, which is itself
         in-sample; a high value may be an artefact of a small sample.

    So it asks a human to look, with the numbers attached. The intended workflow
    is: SUSPECT -> inspect that feature's code -> either fix a leak or write down
    why it is real in the run manifest.

    Implementation note: Pearson correlation is computed with a plain double loop
    and no numpy, matching `core/results.py`. The feature matrices here are small
    (features x rows), so this stays fast and keeps the module dependency-free —
    the whole audit layer must run on a machine with nothing installed.
    """
    out: List[LeakFinding] = []
    y = [v for v in forward_returns]
    if not y:
        return out

    for name, xs in features.items():
        pairs = [(float(a), float(b))
                 for a, b in zip(xs, y)
                 if a is not None and b is not None]
        if len(pairs) < min_obs:
            continue

        n = len(pairs)
        mx = sum(p[0] for p in pairs) / n
        my = sum(p[1] for p in pairs) / n
        cov = sum((p[0] - mx) * (p[1] - my) for p in pairs)
        vx = sum((p[0] - mx) ** 2 for p in pairs)
        vy = sum((p[1] - my) ** 2 for p in pairs)
        denom = (vx * vy) ** 0.5
        if denom == 0:
            continue
        rho = cov / denom

        if abs(rho) >= threshold:
            out.append(LeakFinding(
                "future_correlation", Severity.DEGRADED, name,
                f"SUSPECT: '{name}' correlates {rho:+.3f} with the forward "
                f"return over {n} observations. On daily bars a genuine "
                f"predictor rarely exceeds |0.15|. Inspect this feature for a "
                f"look-ahead (a rolling window without .shift(1) is the usual "
                f"cause) before trusting the backtest.",
                {"rho": round(rho, 4), "n": n}))

    return out


# --------------------------------------------------------------------------- #
# the combined gate
# --------------------------------------------------------------------------- #


def audit_features(
    rows: Sequence[Dict[str, Any]],
    build_features: Optional[Callable[[Sequence[Dict[str, Any]]],
                                      Dict[str, List[Any]]]] = None,
    forward_returns: Optional[Sequence[Optional[float]]] = None,
    *,
    ts_col: str = "as_of_ts",
    raise_on_fatal: bool = True,
) -> Dict[str, Any]:
    """Run every leak check and return a report. Optionally raise on FATAL.

    This is the single function the training orchestrator calls, so there is one
    place where "did we leak?" is decided — not three scattered assertions.

    Args:
        rows: the training dataset, oldest-first.
        build_features: passed to :func:`check_shift_alignment`. Omit to skip the
            behavioural test (the CLI will then warn that only the cheap checks
            ran, so a skip is visible rather than silent).
        forward_returns: passed to :func:`check_future_correlation`.
        ts_col: knowledge-time column name.
        raise_on_fatal: default True. A leak must stop the run. Set False only in
            a test that is deliberately asserting the finding.

    Returns:
        ``{"findings": [...], "fatal": int, "suspect": int, "checks_run": [...],
           "leaked_features": [...]}``

    Raises:
        LeakageError: when a FATAL finding exists and ``raise_on_fatal`` is True.
    """
    findings: List[LeakFinding] = []
    checks_run: List[str] = ["timestamps"]

    findings.extend(check_timestamps(rows, ts_col=ts_col))

    if build_features is not None:
        checks_run.append("shift")
        findings.extend(check_shift_alignment(build_features, rows, ts_col=ts_col))
    else:
        logger.warning("leakage audit: shift test SKIPPED (no build_features). "
                       "Only the cheap timestamp checks ran.")

    if build_features is not None and forward_returns is not None:
        checks_run.append("future_correlation")
        try:
            feats = build_features(rows)
            findings.extend(check_future_correlation(feats, forward_returns))
        except Exception as exc:  # noqa: BLE001
            logger.warning("correlation screen skipped: %r", exc)

    fatal = [f for f in findings if f.severity is Severity.FATAL]
    suspect = [f for f in findings if f.severity is Severity.DEGRADED]

    report = {
        "findings": [f.to_dict() for f in findings],
        "fatal": len(fatal),
        "suspect": len(suspect),
        "checks_run": checks_run,
        "leaked_features": sorted({f.subject for f in fatal}),
        "clean": not fatal and not suspect,
    }

    if fatal:
        logger.error("LEAKAGE AUDIT FAILED: %d fatal finding(s): %s",
                     len(fatal), [f.subject for f in fatal])
        if raise_on_fatal:
            first = fatal[0]
            raise LeakageError(
                f"{len(fatal)} leakage finding(s); first: {first.detail}")
    elif suspect:
        logger.warning("leakage audit: %d suspect feature(s) — review before "
                       "trusting the backtest: %s",
                       len(suspect), [f.subject for f in suspect])
    else:
        logger.info("leakage audit clean (%s)", ", ".join(checks_run))

    return report