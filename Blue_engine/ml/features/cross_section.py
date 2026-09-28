"""
ml/features/cross_section.py — turn a panel of instruments into relative targets.

WHY THIS EXISTS (the statistical argument, verified on this repo's data)
-----------------------------------------------------------------------
A time-series target asks: "will NIFTY rise over the next 5 days?" Measured on
the real cached series (docs/ML_SKILL_DIAGNOSIS.md), that question is nearly
unanswerable from price features — the index rose 54.25% of the time, and no
feature beat that majority class by more than 0.6 percentage points in-sample.

The reason is structural. An index's forward return is

    r_i = market_drift + beta_i * market_move + idiosyncratic_i

and for a single index `market_drift` dominates. Over 2015–2026 NIFTY drifted
~10%/yr, so the majority of any 5-day window is "up". A time-series model has
to predict that drift, and drift is not a feature of last week's RSI.

A CROSS-SECTIONAL target removes it. Instead of "will NIFTY rise?", ask:

    "which of these instruments will rise MOST over the next 5 days?"

The market component is common to every member on a given date, so RANKING
across members subtracts it out. What is left is the relative, idiosyncratic
move — which is where features such as momentum and mean-reversion have their
documented edge (Jegadeesh & Titman 1993 for momentum; the reversal literature
for short-horizon mean reversion).

    # Time-series (what failed): the market term dominates
    label_i = 1 if fwd_return_i > 0

    # Cross-sectional (this module): the market term cancels in the ranking
    label_i = 1 if fwd_return_i ranks in the TOP TIER that date

THE HONEST LIMIT OF THIS IMPLEMENTATION
---------------------------------------
A rank needs enough members to be meaningful. With 3 names, "top tier" is one
name and the label is mostly noise. With 50 names, a tercile is ~17 and the
label is a real cross-sectional statement. This class therefore:

  * requires a configurable minimum width and REFUSES to build a panel that
    is too narrow — returning a reason, not a silently degraded label;
  * records the achievable tier size in the returned provenance, so a model
    trained on a 5-name panel is never presented as if it were 500-name.

The price cache in this repo holds 5 index sleeves on 2,886 shared dates. That
is ENOUGH TO BUILD AND TEST THE MACHINERY, and enough for a coarse top/bottom
split — it is NOT enough for deciles. The class says so in its output rather
than leaving the reader to assume otherwise.

NO LOOK-AHEAD
-------------
Every label here reads FORWARD returns (that is a label's job) and the rank is
computed **within each date independently**. A rank computed across dates — or
using the full sample to standardise — would leak, and the leakage auditor's
shift test catches exactly that. `cross_section_rank` is deliberately written
as a per-date groupby with no cross-date state.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import logger

__all__ = [
    "CrossSectionPanel", "build_panel", "rank_normalise",
    "cross_section_rank", "ranks_to_labels", "MIN_USABLE_WIDTH",
]


#: Below this many members per date a cross-sectional label is mostly noise:
#: a "top tercile" of 2 names is not a cross-sectional statement. Chosen as 5
#: so the smallest honest split is top-2 vs bottom-2 with one in the middle.
MIN_USABLE_WIDTH = 5


class CrossSectionPanel:
    """A date-aligned panel of instrument rows, plus its honesty metadata.

    Attributes:
        dates: sorted list of shared trading dates.
        symbols: the instrument keys present.
        series: ``{symbol: {date: row}}`` — the aligned input.
        width: members per date (the cross-sectional width).
        ok: False when the panel is too narrow to support a ranked label;
            ``reason`` then explains why.
    """

    def __init__(self, dates: List[str], series: Dict[str, Dict[str, Any]],
                 width: int, *, ok: bool = True, reason: Optional[str] = None):
        self.dates = dates
        self.series = series
        self.width = width
        self.ok = ok
        self.reason = reason

    @property
    def symbols(self) -> List[str]:
        return sorted(self.series)

    @property
    def n_dates(self) -> int:
        return len(self.dates)

    @property
    def tier_size(self) -> int:
        """How many members a top tier holds at a 1/3 split (at least 1).

        Reported so a reader can see the resolution of the label. At width 5
        this is 1 — i.e. "the single best instrument that date", which is why
        the docstring is careful not to call it a tercile.
        """
        return max(1, self.width // 3)

    def describe(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "reason": self.reason,
            "n_dates": self.n_dates, "width": self.width,
            "symbols": self.symbols,
            "tier_size": self.tier_size,
            "resolution": ("coarse (single name per date)"
                           if self.tier_size <= 1
                           else f"top {self.tier_size} of {self.width} per date"),
        }


def build_panel(series: Dict[str, Sequence[Dict[str, Any]]], *,
                ts_col: str = "as_of_ts",
                value_col: str = "close",
                min_width: int = MIN_USABLE_WIDTH,
                join: str = "inner") -> CrossSectionPanel:
    """Align several instruments onto their shared dates and check the width.

    Args:
        series: ``{symbol: rows}`` where each row carries `ts_col`.
        min_width: refuse a panel narrower than this. A narrow panel produces a
            label that LOOKS like a cross-sectional one and is not.
        join: "inner" (dates present in EVERY symbol — the only honest choice
            for a cross-section, because a member missing on a date cannot be
            ranked against peers that have it) or "outer".

    Returns:
        A :class:`CrossSectionPanel`. When ``ok`` is False the caller must
        record the reason and NOT train on a degraded label.
    """
    # Index every symbol's rows by day.
    indexed: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for sym, rows in (series or {}).items():
        by_day: Dict[str, Dict[str, Any]] = {}
        for r in rows or []:
            day = str(r.get(ts_col) or "")[:10]
            if not day or r.get(value_col) is None:
                continue
            by_day[day] = r
        if by_day:
            indexed[sym] = by_day

    if len(indexed) < 2:
        return CrossSectionPanel([], {}, 0, ok=False,
                                 reason=(f"need >= 2 instruments with data; "
                                         f"got {len(indexed)}"))

    sets = [set(by_day) for by_day in indexed.values()]
    common = set.intersection(*sets) if join == "inner" else set().union(*sets)
    dates = sorted(common)
    if not dates:
        return CrossSectionPanel([], {}, 0, ok=False,
                                 reason=("no shared dates across "
                                         f"{sorted(indexed)} — the instruments "
                                         f"do not overlap in time"))

    # Realised width: the MEDIAN count of members with a value per date. Using
    # the minimum would let one sparse date veto an otherwise usable panel;
    # using the max would overstate it. The median is the honest summary.
    counts: List[int] = []
    for d in dates:
        counts.append(sum(1 for sym in indexed if indexed[sym].get(d) is not None))
    counts.sort()
    width = counts[len(counts) // 2] if counts else 0

    if width < min_width:
        return CrossSectionPanel(
            dates, {s: {d: indexed[s][d] for d in dates if d in indexed[s]}
                    for s in indexed},
            width, ok=False,
            reason=(f"median width {width} < min_width {min_width}: a ranked "
                    f"label at this width is mostly noise. Add instruments "
                    f"(more symbols in the price cache) or lower min_width "
                    f"consciously."))

    panel = CrossSectionPanel(
        dates, {s: {d: indexed[s][d] for d in dates if d in indexed[s]}
                for s in indexed},
        width)
    logger.info("cross-section panel: %d instruments x %d shared dates "
                "(width %d, %s)", len(panel.symbols), panel.n_dates,
                panel.width, panel.describe()["resolution"])
    return panel


def cross_section_rank(values_by_symbol: Dict[str, float]) -> Dict[str, float]:
    """Fractional rank of each value WITHIN one date, in [0, 1].

    1.0 = highest value that date, 0.0 = lowest. Ties share the average rank,
    so two identical values cannot be ordered arbitrarily (which would inject
    noise into the label).

    The function takes ONE date's values. That signature is the leak guard:
    there is no way to rank across dates by accident, because it cannot see
    them.
    """
    items = [(s, v) for s, v in values_by_symbol.items()
             if v is not None and v == v]
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 0.5}
    order = sorted(items, key=lambda kv: kv[1])
    ranks: Dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and order[j + 1][1] == order[i][1]:
            j += 1                      # extend the tie group
        # Average rank of the tie group, scaled into (0, 1].
        avg = (i + j) / 2.0
        frac = (avg + 0.5) / n
        for k in range(i, j + 1):
            ranks[order[k][0]] = frac
        i = j + 1
    return ranks


def ranks_to_labels(ranks: Dict[str, float], *, top_frac: float = 1.0 / 3.0,
                    bottom_frac: float = 1.0 / 3.0
                    ) -> Tuple[Dict[str, Optional[int]], Dict[str, Any]]:
    """Map within-date ranks to 1 (top tier), 0 (bottom tier), None (middle).

    The middle is **dropped, not forced into a class**. Forcing it would invent
    a label for the rows where the cross-section says nothing, which dilutes
    the signal the target exists to capture.

    Args:
        top_frac / bottom_frac: tier sizes as a fraction of the width. Both
            default to 1/3, leaving a third unlabelled.

    Returns:
        ``(labels, meta)`` where meta records how many rows each class got and
        the tier sizes actually achieved — so a two-name tier is visible as
        such rather than presented as a tercile.
    """
    if not ranks:
        return {}, {"n_top": 0, "n_bottom": 0, "tier_size": 0}
    n = len(ranks)
    ordered = sorted(ranks.items(), key=lambda kv: kv[1], reverse=True)
    n_top = max(1, int(math.ceil(n * top_frac)))
    n_bottom = max(1, int(math.ceil(n * bottom_frac)))
    if n_top + n_bottom > n:
        # A very narrow panel: halve so the two tiers cannot overlap.
        n_top = n_bottom = max(1, n // 2)

    labels: Dict[str, Optional[int]] = {s: None for s in ranks}
    for sym, _r in ordered[:n_top]:
        labels[sym] = 1
    for sym, _r in ordered[n - n_bottom:]:
        labels[sym] = 0
    meta = {"n_top": n_top, "n_bottom": n_bottom, "tier_size": n_top,
            "n_total": n, "n_unlabelled": sum(1 for v in labels.values()
                                              if v is None)}
    return labels, meta


def build_cross_sectional_dataset(
        series: Dict[str, Sequence[Dict[str, Any]]], *,
        horizon: int = 5,
        close_col: str = "close",
        ts_col: str = "as_of_ts",
        min_width: int = MIN_USABLE_WIDTH,
        top_frac: float = 1.0 / 3.0,
        bottom_frac: float = 1.0 / 3.0,
) -> Dict[str, Any]:
    """The full cross-sectional dataset: features per member + relative labels.

    Rows are emitted per (date, symbol). The label compares the member's own
    FORWARD return against its peers on the same date, so the market component
    cancels — see the module docstring for why that is the point.

    Returns a dict with the panel, the feature matrix, the labels and the
    forward returns, plus an ``ok``/``reason`` pair. On failure ``ok`` is False
    and every other key is empty, so a caller cannot train on a partial panel.
    """
    from . import engineering, labels as labels_mod

    panel = build_panel(series, ts_col=ts_col, value_col=close_col,
                        min_width=min_width)
    if not panel.ok:
        logger.warning("cross-section not built: %s", panel.reason)
        return {"ok": False, "reason": panel.reason, "panel": panel.describe(),
                "X": [], "y": [], "feature_names": [], "rows": [],
                "forward_returns": [], "dates": [], "meta": {}}

    # Per-symbol features and forward returns, computed on that symbol's own
    # series BEFORE alignment. Each symbol's features are a function of its own
    # past only, so this introduces no cross-member leak.
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for sym in panel.symbols:
        rows_s = [panel.series[sym][d] for d in panel.dates]
        feats = engineering.build_price_features(
            rows_s, close_col=close_col, ts_col=ts_col)
        fwd = labels_mod.forward_return(
            [None if r.get(close_col) is None else float(r[close_col])
             for r in rows_s], horizon)
        per_symbol[sym] = {"feats": feats, "fwd": fwd, "rows": rows_s}

    feature_names = sorted(per_symbol[panel.symbols[0]]["feats"])

    X: List[List[float]] = []
    y: List[Optional[int]] = []
    fwd_out: List[Optional[float]] = []
    row_meta: List[Dict[str, Any]] = []
    dates_out: List[str] = []
    tier_meta: Dict[str, Any] = {}

    for di, day in enumerate(panel.dates):
        # --- the cross-sectional step: rank members by forward return ---
        fwd_by_sym: Dict[str, float] = {}
        for sym in panel.symbols:
            f = per_symbol[sym]["fwd"][di]
            if f is not None:
                fwd_by_sym[sym] = float(f)
        if len(fwd_by_sym) < min_width:
            continue                    # too few ranked members this date
        ranks = cross_section_rank(fwd_by_sym)
        lab, meta = ranks_to_labels(ranks, top_frac=top_frac,
                                    bottom_frac=bottom_frac)
        tier_meta = meta

        for sym in panel.symbols:
            lbl = lab.get(sym)
            if lbl is None:
                continue                # middle tier: no cross-sectional claim
            vals = [per_symbol[sym]["feats"][k][di] for k in feature_names]
            if any(v is None for v in vals):
                continue
            X.append([float(v) for v in vals])
            y.append(int(lbl))
            fwd_out.append(fwd_by_sym.get(sym))
            row_meta.append({"date": day, "symbol": sym,
                             "rank": ranks.get(sym)})
            dates_out.append(day)

    if not X:
        reason = ("no rows survived the cross-sectional build — check that "
                  "features are available before the shared dates begin")
        return {"ok": False, "reason": reason, "panel": panel.describe(),
                "X": [], "y": [], "feature_names": feature_names, "rows": [],
                "forward_returns": [], "dates": [], "meta": {}}

    bal_known = [v for v in y if v is not None]
    base = (sum(bal_known) / len(bal_known)) if bal_known else None
    logger.info("cross-section dataset: %d rows, %d instruments, base_rate=%s",
                len(X), len(panel.symbols), base)

    return {
        "ok": True, "reason": None,
        "panel": panel.describe(),
        "X": X, "y": y, "feature_names": feature_names,
        "rows": row_meta, "dates": dates_out,
        "forward_returns": fwd_out,
        "meta": {
            "kind": "cross_sectional",
            "horizon": horizon,
            "width": panel.width,
            "tier": tier_meta,
            "base_rate": None if base is None else round(base, 4),
            "n_rows": len(X),
        },
    }


def rank_normalise(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Map a series to its fractional rank in [0, 1] (cross-date).

    OFFERED FOR FEATURE CONSTRUCTION ONLY — and it is a *feature* helper, not a
    label helper. Ranking a series against its OWN full history is a full-sample
    operation: it uses future values to scale past ones. That is exactly the
    leak `engineering.zscore_latest` is named to warn about, so this function
    carries the same warning and the same restriction: never a model input
    unless computed on a trailing window.

    Provided so a caller reaching for "normalise this" has a documented,
    correctly-named place to find it rather than writing an unnamed version.
    """
    known = sorted(v for v in values if v is not None)
    n = len(known)
    if n == 0:
        return [None] * len(values)
    out: List[Optional[float]] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        lo = 0
        hi = n
        while lo < hi:                  # bisect_left: rank position
            mid = (lo + hi) // 2
            if known[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        out.append((lo + 0.5) / n)
    return out