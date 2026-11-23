"""
ml/audit/quality.py — the data-quality gate that runs before any training.

WHY A GATE AND NOT A SET OF ASSERTS
-----------------------------------
Every ML bug that has ever cost this desk money arrived as *plausible* data, not
as an exception. A vendor returns 400 rows where 1,200 were expected; a holiday
week is missing; the volume column silently changes units; the feed freezes and
serves yesterday's values. All of these train a model happily.

The gate turns each of those into a named, quantified, visible finding. It is
deliberately *strict by default*: a run that cannot prove its data is sound does
not produce an artifact. The cost is an occasional blocked run. The alternative
cost is a model you trust and should not.

THE CHECKS
----------
1. **Non-empty**            — no rows at all is FATAL.
2. **Required columns**     — a missing column is FATAL (usually a schema change).
3. **Sufficient history**   — fewer than `MIN_TRAIN_DAYS` distinct dates is
                             DEGRADED for research, FATAL when `production=True`.
4. **Duplicate timestamps** — two rows for one instant is DEGRADED.
5. **Missing-value fraction** — a column above `MAX_MISSING_FRAC` is DEGRADED; a
                             *day* with too many nulls across columns is flagged
                             as a suspected partial outage.
6. **Gaps**                 — the largest jump between consecutive dates. NSE has
                             long weekends and a handful of multi-day holidays,
                             so a gap is only flagged when it exceeds
                             `max_gap_days` (default 10 calendar days).
7. **Staleness**            — the newest row's age against `max_stale_days`.
                             A model trained on year-old data is not a model of
                             today's market.
8. **Price sanity**         — non-positive closes, and single-bar moves beyond
                             `max_abs_return` (default 50%) are flagged. A 50%
                             one-day move in an INDEX is almost always a data
                             error, not a market event — and it will dominate any
                             volatility estimate.

CALIBRATION NOTES (why these specific numbers)
----------------------------------------------
* `MIN_TRAIN_DAYS = 750` (~3 years of trading days). Enough to span a full
  bull/bear cycle on NIFTY so a model is not fit to one regime. Below that, a
  walk-forward has too few folds for its own confidence interval to mean anything.
* `MAX_MISSING_FRAC = 0.20`. Above 20% nulls a column is usually mis-parsed
  rather than merely patchy, and imputation starts inventing the signal.
* `max_gap_days = 10`. NSE's longest regular closure (Diwali + weekend clusters)
  stays under a week; anything longer is a missing file, not a holiday.
* `max_abs_return = 0.50`. Large indices essentially never move 50% in a day.
  Individual small caps can, so this is configurable per source.

Everything here is pure: no numpy, no pandas, no I/O beyond reading the rows it
is handed. That keeps the gate usable in a bare environment and trivially
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from .. import config as ml_config
from .. import logger
from .degradation import Severity
from .leakage import LeakFinding

__all__ = ["QualityReport", "check_quality", "DEFAULT_MAX_GAP_DAYS"]


#: Longest acceptable calendar gap between consecutive observations.
DEFAULT_MAX_GAP_DAYS = 10

#: A one-bar absolute return above this is treated as a probable data error.
DEFAULT_MAX_ABS_RETURN = 0.50


@dataclass
class QualityReport:
    """The gate's verdict. Feeds both the training decision and the dashboard."""

    ok: bool = True
    n_rows: int = 0
    n_dates: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    findings: List[LeakFinding] = field(default_factory=list)
    #: Per-column null fraction, so the dashboard can show *which* column is bad
    #: rather than just "data quality: fail".
    missing_frac: Dict[str, float] = field(default_factory=dict)

    @property
    def fatal(self) -> List[LeakFinding]:
        return [f for f in self.findings if f.severity is Severity.FATAL]

    @property
    def degraded(self) -> List[LeakFinding]:
        return [f for f in self.findings if f.severity is Severity.DEGRADED]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "n_rows": self.n_rows,
            "n_dates": self.n_dates,
            "first_date": self.first_date,
            "last_date": self.last_date,
            "fatal": [f.to_dict() for f in self.fatal],
            "degraded": [f.to_dict() for f in self.degraded],
            "missing_frac": {k: round(v, 4)
                             for k, v in sorted(self.missing_frac.items())},
        }

    def summary_line(self) -> str:
        """One line for a log or a dashboard badge."""
        if self.fatal:
            return (f"FAIL — {len(self.fatal)} fatal, {len(self.degraded)} "
                    f"degraded ({self.n_rows} rows, {self.first_date}.."
                    f"{self.last_date})")
        if self.degraded:
            return (f"WARN — {len(self.degraded)} degraded "
                    f"({self.n_rows} rows, {self.first_date}..{self.last_date})")
        return (f"OK — {self.n_rows} rows, {self.first_date}..{self.last_date}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _day_of(value: Any) -> Optional[str]:
    """Extract a YYYY-MM-DD day from any timestamp shape, or None.

    Accepts ISO strings, date/datetime objects. Returns None rather than raising
    so a single malformed stamp becomes a finding instead of an exception.
    """
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _to_day(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value[:10])
    except Exception:  # noqa: BLE001
        return None


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        f = float(value)
        return f if f == f else None          # drop NaN
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def check_quality(
    rows: Sequence[Dict[str, Any]],
    *,
    ts_col: str = "as_of_ts",
    value_cols: Optional[Sequence[str]] = None,
    required_columns: Sequence[str] = (),
    min_rows: Optional[int] = None,
    production: bool = False,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    max_missing_frac: Optional[float] = None,
    max_stale_days: Optional[int] = None,
    max_abs_return: float = DEFAULT_MAX_ABS_RETURN,
    today: Optional[str] = None,
) -> QualityReport:
    """Run every quality check. Pure; never raises.

    Args:
        rows: the dataset, ideally oldest-first.
        ts_col: knowledge-time column.
        value_cols: numeric columns to profile for nulls and sanity. Defaults to
            a sensible guess (`close` when present, else nothing) so the common
            price case needs no configuration.
        required_columns: columns that must exist (mirrors the source's own
            `required_columns`, so the gate and the source agree).
        min_rows: distinct-date minimum. Defaults to
            :data:`ml_config.MIN_TRAIN_DAYS`.
        production: when True, a short history becomes FATAL instead of DEGRADED.
            Research notebooks may explore a short sample; a production artifact
            may not be built on one.
        max_gap_days: largest tolerated calendar gap.
        max_missing_frac: per-column null fraction ceiling. Defaults to
            :data:`ml_config.MAX_MISSING_FRAC`.
        max_stale_days: newest-row age ceiling. None disables the check (useful
            for a deliberately historical research panel).
        max_abs_return: one-bar absolute return considered impossible.
        today: override "today" for deterministic tests.

    Returns:
        A :class:`QualityReport`. ``report.ok`` is False when any FATAL exists.
    """
    min_rows = int(min_rows if min_rows is not None
                   else ml_config.MIN_TRAIN_DAYS)
    max_missing_frac = float(max_missing_frac if max_missing_frac is not None
                             else ml_config.MAX_MISSING_FRAC)

    rep = QualityReport(n_rows=len(rows))

    # ---- 1. non-empty ---------------------------------------------------- #
    if not rows:
        rep.findings.append(LeakFinding(
            "quality", Severity.FATAL, "<dataset>",
            "dataset has zero rows. Nothing to train on — this is a data "
            "acquisition failure, not a modelling one."))
        rep.ok = False
        return rep

    # ---- 2. required columns --------------------------------------------- #
    for col in required_columns:
        absent = sum(1 for r in rows if col not in r)
        if absent:
            rep.findings.append(LeakFinding(
                "quality", Severity.FATAL, col,
                f"required column '{col}' missing from {absent} of "
                f"{len(rows)} rows. This is usually a vendor schema change — "
                f"the model would be trained on a different table than it "
                f"expects.", {"absent_rows": absent}))

    # ---- dates ----------------------------------------------------------- #
    days = [_day_of(r.get(ts_col)) for r in rows]
    valid_days = [d for d in days if d]
    rep.n_dates = len(set(valid_days))
    if valid_days:
        rep.first_date, rep.last_date = min(valid_days), max(valid_days)

    # ---- 3. sufficient history ------------------------------------------- #
    if rep.n_dates and rep.n_dates < min_rows:
        sev = Severity.FATAL if production else Severity.DEGRADED
        rep.findings.append(LeakFinding(
            "quality", sev, "<history>",
            f"only {rep.n_dates} distinct date(s); {min_rows} required"
            f"{' for a production artifact' if production else ''}. A "
            f"walk-forward needs enough folds for its confidence interval to be "
            f"meaningful, and a short sample fits one regime only.",
            {"n_dates": rep.n_dates, "required": min_rows}))

    # ---- 4. duplicate timestamps ----------------------------------------- #
    if len(valid_days) != len(set(valid_days)):
        dupes = len(valid_days) - len(set(valid_days))
        rep.findings.append(LeakFinding(
            "quality", Severity.DEGRADED, ts_col,
            f"{dupes} duplicate date(s). A duplicate bar double-counts that day "
            f"in every rolling window and can double-count it in a backtest P&L.",
            {"duplicates": dupes}))

    # ---- 5. missing values ----------------------------------------------- #
    cols = list(value_cols) if value_cols else (
        ["close"] if "close" in rows[0] else [])
    for col in cols:
        nulls = sum(1 for r in rows if _num(r.get(col)) is None)
        frac = nulls / len(rows)
        rep.missing_frac[col] = frac
        if frac > max_missing_frac:
            rep.findings.append(LeakFinding(
                "quality", Severity.DEGRADED, col,
                f"'{col}' is {frac:.1%} missing (limit "
                f"{max_missing_frac:.0%}). Above this level the column is "
                f"usually mis-parsed rather than patchy, and imputing it "
                f"invents the signal.",
                {"missing_frac": round(frac, 4), "n_missing": nulls}))

    # ---- 6. gaps ---------------------------------------------------------- #
    ordered = sorted({d for d in valid_days})
    parsed = [(d, _to_day(d)) for d in ordered]
    parsed = [(d, p) for d, p in parsed if p is not None]
    worst_gap, worst_span = 0, None
    for (d1, p1), (d2, p2) in zip(parsed, parsed[1:]):
        span = (p2 - p1).days
        if span > worst_gap:
            worst_gap, worst_span = span, (d1, d2)
    if worst_span and worst_gap > max_gap_days:
        rep.findings.append(LeakFinding(
            "quality", Severity.DEGRADED, "<gaps>",
            f"largest calendar gap is {worst_gap} days "
            f"({worst_span[0]} -> {worst_span[1]}), above the "
            f"{max_gap_days}-day limit. NSE's longest regular closure stays "
            f"under a week, so this is usually a missing file or a failed "
            f"window rather than a holiday.",
            {"gap_days": worst_gap, "from": worst_span[0], "to": worst_span[1]}))

    # ---- 7. staleness ----------------------------------------------------- #
    if max_stale_days is not None and rep.last_date:
        ref = today or date.today().strftime("%Y-%m-%d")
        ref_d, last_d = _to_day(ref), _to_day(rep.last_date)
        if ref_d and last_d:
            age = (ref_d - last_d).days
            if age > int(max_stale_days):
                rep.findings.append(LeakFinding(
                    "quality", Severity.DEGRADED, "<staleness>",
                    f"newest row is {age} days old (limit {max_stale_days}). "
                    f"A model trained on stale data describes a market that no "
                    f"longer exists.",
                    {"age_days": age, "last_date": rep.last_date}))

    # ---- 8. price sanity -------------------------------------------------- #
    if "close" in rows[0]:
        closes = [_num(r.get("close")) for r in rows]
        nonpositive = sum(1 for c in closes if c is not None and c <= 0)
        if nonpositive:
            rep.findings.append(LeakFinding(
                "quality", Severity.FATAL, "close",
                f"{nonpositive} non-positive close value(s). A zero or negative "
                f"price makes every return calculation undefined and can produce "
                f"an infinite return.",
                {"n_nonpositive": nonpositive}))

        jumps: List[Dict[str, Any]] = []
        prev: Optional[float] = None
        for r, c in zip(rows, closes):
            if c is not None and prev is not None and prev > 0:
                ret = c / prev - 1.0
                if abs(ret) > max_abs_return:
                    jumps.append({"ts": _day_of(r.get(ts_col)),
                                  "ret": round(ret, 4)})
            if c is not None:
                prev = c
        if jumps:
            rep.findings.append(LeakFinding(
                "quality", Severity.DEGRADED, "close",
                f"{len(jumps)} one-bar move(s) exceed "
                f"{max_abs_return:.0%}. For an index this is almost always a "
                f"data error (a bad tick, a stale print, a unit change), and it "
                f"will dominate any volatility estimate built from the series. "
                f"First: {jumps[0]}",
                {"n_jumps": len(jumps), "first": jumps[0]}))

    rep.ok = not rep.fatal
    logger.info("quality gate: %s", rep.summary_line())
    return rep


def require_quality(rep: QualityReport) -> None:
    """Raise ValueError when the gate failed. For callers that want a hard stop.

    Kept separate from :func:`check_quality` so the gate can also be used in
    "report only" mode (the dashboard does exactly that) without forcing a raise.
    """
    if rep.ok:
        return
    first = rep.fatal[0] if rep.fatal else None
    raise ValueError(
        f"data quality gate failed: {rep.summary_line()}"
        + (f"; first fatal: {first.detail}" if first else ""))