"""Data QA gate. Any ERROR stops the run before signals are made.

Flow stage: Data QA gate
Status: PHASE 1 IMPLEMENTED; engine checks wrapped 2026-11-14
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/audit/quality.py
    (check_quality, QualityReport) - the eight-check gate, wrapped by
    ``audit_bars`` below.

TWO ENTRY POINTS, ONE RULE
--------------------------
``check_bars(bars, actions)`` is the original DataFrame gate and keeps its exact
signature and result type, because ``scripts/run_backtest.py`` and
``test_phase1_data_run.py`` depend on it.

``audit_bars(bars, ...)`` is the stricter gate: it converts the frame to rows and
runs the ENGINE's eight checks, which cover four things ``check_bars`` did not:

  1. calendar gaps (a missing Diwali week used to pass silently),
  2. insufficient history (``MIN_TRAIN_DAYS``),
  3. per-column missing-value fraction,
  4. data staleness against today.

``check_bars`` now ALSO reports the calendar-gap condition, because a hole in the
series is the one failure that corrupts every downstream estimate while looking
perfectly healthy. It stays an ERROR so an existing caller still sees ``passed``
go false exactly when it should.

SEVERITY
--------
The engine uses FATAL / DEGRADED; this module's own type uses ERROR / WARNING.
``to_qa_report`` maps them, keeping this module's long-standing vocabulary so a
caller is not forced to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from quant.core.blue_bridge import BlueEngineUnavailable, load

REQUIRED = ["date", "symbol", "open", "high", "low", "close", "volume"]
STALE_RUN = 5
MAX_OVERNIGHT_MOVE = 0.30

#: Longest tolerated calendar gap between consecutive bars for one symbol.
#: NSE's longest regular closure (Diwali clusters) stays under a week.
MAX_GAP_DAYS = 10

#: Engine severity -> this module's severity.
_SEVERITY_MAP = {"fatal": "ERROR", "degraded": "WARNING"}


@dataclass
class QAReport:
    issues: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(level == "ERROR" for level, _ in self.issues)

    def add(self, level: str, message: str) -> None:
        self.issues.append((level, message))


def _calendar_gap_issues(bars: pd.DataFrame, max_gap_days: int = MAX_GAP_DAYS) -> List[str]:
    """Symbols whose consecutive dates jump more than ``max_gap_days``.

    A gap means rows are MISSING, not that the market was calm. Every rolling
    window, every return series and every volatility estimate silently spans the
    hole, so this is reported as an error rather than a warning.
    """
    out: List[str] = []
    if "date" not in bars.columns or "symbol" not in bars.columns:
        return out
    try:
        dates = pd.to_datetime(bars["date"], errors="coerce")
    except Exception:  # noqa: BLE001 - unparseable dates are caught elsewhere
        return out
    frame = pd.DataFrame({"symbol": bars["symbol"], "date": dates}).dropna()
    for sym, g in frame.sort_values("date").groupby("symbol"):
        deltas = g["date"].diff().dt.days.dropna()
        if len(deltas) and deltas.max() > max_gap_days:
            out.append(
                f"{sym}: calendar gap of {int(deltas.max())} days "
                f"(limit {max_gap_days}) - rows are missing, not a market holiday run")
    return out


def check_bars(bars: pd.DataFrame, actions: pd.DataFrame = None) -> QAReport:
    """DataFrame gate. actions: symbol, date. Overnight moves on those dates are expected.

    Unchanged from the original contract, plus the calendar-gap check. Still
    raises nothing and still returns ``QAReport``.
    """
    rep = QAReport()
    missing = [c for c in REQUIRED if c not in bars.columns]
    if missing:
        rep.add("ERROR", f"missing columns: {missing}")
        return rep
    if bars.duplicated(["date", "symbol"]).any():
        rep.add("ERROR", "duplicate date+symbol rows")
    if bars[["open", "high", "low", "close"]].isna().any().any():
        rep.add("ERROR", "NaN prices")
    if (bars[["open", "high", "low", "close"]] <= 0).any().any():
        rep.add("ERROR", "non-positive prices")
    if (bars["high"] < bars["low"]).any():
        rep.add("ERROR", "high below low")
    bad_range = (bars["close"] > bars["high"]) | (bars["close"] < bars["low"])
    if bad_range.any():
        rep.add("ERROR", "close outside high/low")

    for msg in _calendar_gap_issues(bars):
        rep.add("ERROR", msg)

    df = bars.sort_values(["symbol", "date"])
    for sym, g in df.groupby("symbol"):
        closes = g["close"].to_numpy()
        run = 1
        for i in range(1, len(closes)):
            run = run + 1 if closes[i] == closes[i - 1] else 1
            if run == STALE_RUN:
                rep.add("WARNING", f"{sym}: stale close for {STALE_RUN}+ bars")
                break
        move = np.abs(g["close"].pct_change().to_numpy())
        dates = g["date"].to_numpy()
        for i in np.where(move > MAX_OVERNIGHT_MOVE)[0]:
            if actions is None or not ((actions["symbol"] == sym) & (actions["date"] == dates[i])).any():
                rep.add("ERROR", f"{sym}: unexplained move > {MAX_OVERNIGHT_MOVE:.0%} on {dates[i]}")
    if (bars["volume"] == 0).any():
        rep.add("WARNING", "zero-volume bars present")
    return rep


# --------------------------------------------------------------------------- #
# the engine gate (Stage 1.2, wrapped)
# --------------------------------------------------------------------------- #
def _module():
    try:
        return load("ml.audit.quality")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise QAUnavailable(f"engine quality gate unavailable: {exc}") from exc


class QAUnavailable(RuntimeError):
    """The engine's data-quality gate cannot be loaded."""


def audit_bars(
    bars: pd.DataFrame,
    *,
    ts_col: str = "date",
    production: bool = False,
    min_rows: Optional[int] = None,
    max_gap_days: int = MAX_GAP_DAYS,
    max_stale_days: Optional[int] = None,
    max_abs_return: float = MAX_OVERNIGHT_MOVE,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the ENGINE's eight-check gate over a bars frame.

    Returns the engine report as a dict, plus a ``verdict`` this pipeline can
    branch on: ``clean`` / ``degraded`` / ``failed`` / ``unavailable``.

    ``max_stale_days`` defaults to None so a deliberately historical research
    panel is not refused for being old. Pass an int to enforce freshness.
    """
    try:
        mod = _module()
    except QAUnavailable as exc:
        return {"verdict": "unavailable", "ok": False, "detail": str(exc),
                "fatal": [], "degraded": [], "missing_frac": {},
                "n_rows": 0, "n_dates": 0}

    rows = bars.to_dict("records")
    report = mod.check_quality(
        rows,
        ts_col=ts_col,
        value_cols=["close"] if "close" in bars.columns else None,
        required_columns=[c for c in REQUIRED if c != ts_col and c not in ("symbol",)],
        min_rows=min_rows,
        production=production,
        max_gap_days=max_gap_days,
        max_stale_days=max_stale_days,
        max_abs_return=max_abs_return,
        today=today,
    )
    data = report.to_dict()
    if data.get("fatal"):
        verdict = "failed"
    elif data.get("degraded"):
        verdict = "degraded"
    else:
        verdict = "clean"
    return {**data, "verdict": verdict, "summary": report.summary_line()}


def to_qa_report(engine_report: Dict[str, Any]) -> QAReport:
    """Convert an ``audit_bars`` result into this module's ``QAReport``.

    Lets a caller keep one result type while gaining the engine's coverage.
    """
    rep = QAReport()
    if engine_report.get("verdict") == "unavailable":
        rep.add("ERROR", str(engine_report.get("detail") or "quality gate unavailable"))
        return rep
    for finding in engine_report.get("fatal") or []:
        rep.add("ERROR", f"{finding.get('check')}: {finding.get('detail')}")
    for finding in engine_report.get("degraded") or []:
        rep.add("WARNING", f"{finding.get('check')}: {finding.get('detail')}")
    return rep
