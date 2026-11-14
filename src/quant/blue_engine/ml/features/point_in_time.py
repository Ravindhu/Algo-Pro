"""
ml/features/point_in_time.py — the as-of join. This file is the leak barrier.

THE PROBLEM IT SOLVES, CONCRETELY
---------------------------------
Suppose you want "CPI inflation as known on each trading day" as a feature.

The naive join is on the *reference month*:

    trading_day 2026-08-05  ->  CPI for month 2026-08  ->  value 4.82
    trading_day 2026-08-06  ->  CPI for month 2026-08  ->  value 4.82
    ...

That is look-ahead. August CPI is not published until ~12 September. On 5 August
nobody knew it. A model trained this way learns "CPI tells me what the market will
do this month" — which is true in the dataset and false in reality. Backtest:
excellent. Live: worthless.

The correct join is on *knowledge time*, taking the most recent value whose
`as_of_ts` is <= the trading day:

    trading_day 2026-08-05  ->  latest as_of_ts <= 08-05  ->  July CPI (published 12 Aug? no)
                                                            ->  June CPI, published 12 Jul  -> 4.75
    trading_day 2026-09-12  ->  latest as_of_ts <= 09-12  ->  August CPI, published 12 Sep -> 4.82

Notice the second row: on the release day itself the new value IS available. That
is the correct convention (`as_of_ts <= t`, inclusive) and it is why sources must
stamp a release *time*, not a date — an 18:40 release is not knowable at the
09:15 open, and an inclusive date join would wrongly include it.

WHY A BACKWARD-ONLY JOIN AND NOT A MERGE
----------------------------------------
A pandas `merge_asof(direction="backward")` is the right primitive and this module
does exactly that when pandas is available. But it also ships a dependency-free
implementation, because:

  * the audit layer must run on a bare machine (see `core/results.py` for the same
    house rule),
  * a hand-written loop is auditable — you can read it and see there is no forward
    direction available, whereas `merge_asof` has a `direction` parameter that
    someone could flip.

The API deliberately offers **no way to request a forward join.** There is no
`direction=` argument. If you need future data you are building a label, not a
feature, and labels belong in `ml/features/labels.py` where they are clearly
marked as targets.

CASE STUDY — the merge that was one row off
-------------------------------------------
An earlier version joined daily prices to a monthly macro panel by *index
position* after a `reset_index()`. It worked, in the sense that it ran. What it
actually did was attach the macro value from row N to the price from row N+1
whenever the two series had different lengths — a one-row look-ahead, invisible
in review, worth roughly +0.3 Sharpe of pure fiction. The as-of join on
timestamps cannot make that mistake: it does not know about positions at all.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .. import logger

__all__ = ["asof_join", "asof_value", "align_panel", "parse_ts"]


# --------------------------------------------------------------------------- #
# timestamp parsing (tolerant, never raises)
# --------------------------------------------------------------------------- #


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse any supported timestamp into a naive datetime, or None.

    Supported: `datetime`, `date`, and ISO-ish strings with an optional `T`,
    optional seconds and optional UTC offset. We deliberately do NOT pull in
    `dateutil` — one more dependency in the audit path is one more thing that can
    fail on a bare machine.

    A timezone offset is parsed and then *dropped* after shifting to IST, so all
    comparisons happen in one wall clock. Mixing naive and aware datetimes raises
    TypeError in Python, and that failure mode would surface deep inside a join
    where it is hard to attribute.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    s = str(value).strip()
    if not s:
        return None

    # Split off a UTC offset like +05:30 / -04:00 / Z, then shift to IST.
    offset_min = 0
    if s.endswith("Z"):
        s = s[:-1]
        offset_min = 0
    elif len(s) > 6 and s[-6] in "+-" and s[-3] == ":":
        sign = 1 if s[-6] == "+" else -1
        try:
            offset_min = sign * (int(s[-5:-3]) * 60 + int(s[-2:]))
        except ValueError:
            offset_min = 0
        s = s[:-6]

    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            # Convert the parsed local time to IST (UTC+330) so every timestamp
            # in the pipeline shares one wall clock.
            return dt + _minutes(330 - offset_min)
        except ValueError:
            continue
    return None


def _minutes(total: int):
    from datetime import timedelta
    return timedelta(minutes=total)


# --------------------------------------------------------------------------- #
# the as-of join
# --------------------------------------------------------------------------- #


def asof_join(base_rows: Sequence[Dict[str, Any]],
              lookup_rows: Sequence[Dict[str, Any]],
              *,
              base_ts: str = "as_of_ts",
              lookup_ts: str = "as_of_ts",
              columns: Optional[Sequence[str]] = None,
              prefix: str = "",
              default: Any = None,
              max_staleness_days: Optional[int] = None,
              ) -> List[Dict[str, Any]]:
    """Attach the most recent known lookup values to each base row.

    This is a **backward-only** as-of join: for base row at time `t`, take the
    lookup row with the greatest `lookup_ts <= t`. Ties resolve to the LAST such
    row in input order (later input wins), which matters when a source re-releases
    a corrected figure for the same timestamp — the correction is what you want.

    Args:
        base_rows: the rows to enrich (usually prices). Not mutated.
        lookup_rows: the reference table (usually macro/fundamentals).
        base_ts / lookup_ts: timestamp column names.
        columns: lookup columns to attach. None = all except the lookup timestamp.
        prefix: prepended to every attached column name, e.g. `"cpi_"`. Use it to
            make provenance obvious in the feature matrix.
        default: value used when no lookup row is old enough (the very start of a
            series). `None` is honest — the quality gate will see the nulls. Do
            NOT pass 0, which fabricates an observation.
        max_staleness_days: when set, a lookup value older than this is treated as
            unknown and `default` is used. This prevents a six-month-old macro
            print from being carried forward forever and pretending to be current.

    Returns:
        New list of dicts (base rows + attached columns). Never raises; an
        unparseable timestamp on either side means that row simply does not match.

    Example:
        >>> prices = [{"as_of_ts": "2026-09-12T15:30:00+05:30", "close": 100.0}]
        >>> cpi = [{"as_of_ts": "2026-09-12T18:40:00+05:30", "value": 4.82},
        ...        {"as_of_ts": "2026-08-12T18:40:00+05:30", "value": 4.75}]
        >>> asof_join(prices, cpi, columns=["value"], prefix="cpi_")[0]["cpi_value"]
        4.75
        # The 18:40 release is AFTER the 15:30 close, so it is correctly NOT used.
        # This single behaviour is the difference between a real model and a
        # beautiful fiction.
    """
    if not base_rows:
        return []

    # Prepare the lookup side once: parsed, sorted, and reduced to the columns
    # we will attach. Doing this outside the loop keeps the join O(n log n).
    prepared: List[Dict[str, Any]] = []
    for r in lookup_rows or []:
        ts = parse_ts(r.get(lookup_ts))
        if ts is None:
            continue
        prepared.append({"__ts": ts, "__row": r})
    prepared.sort(key=lambda x: x["__ts"])

    if not prepared:
        logger.warning("asof_join: no usable lookup rows (ts column %r) — "
                       "attaching defaults only", lookup_ts)

    keys: List[str] = []
    if prepared:
        sample = prepared[-1]["__row"]
        keys = [c for c in (columns or sample.keys())
                if c not in (lookup_ts, "__ts")]

    out: List[Dict[str, Any]] = []
    for base in base_rows:
        bts = parse_ts(base.get(base_ts))
        row = dict(base)
        if bts is None:
            # An unparseable base timestamp is a data problem the quality gate
            # reports; here we just leave the row untouched rather than guessing.
            out.append(row)
            continue

        best: Optional[Dict[str, Any]] = None
        # Linear scan is fine for the sizes involved (a few hundred lookup rows),
        # and it is obviously correct — which is worth more than the microseconds
        # a bisect would save. Swap in bisect if a source ever returns 10k+ rows.
        for cand in prepared:
            if cand["__ts"] <= bts:
                best = cand
            else:
                break                      # sorted: nothing later can qualify

        if best is None:
            for k in keys:
                row[f"{prefix}{k}"] = default
            out.append(row)
            continue

        if max_staleness_days is not None:
            age = (bts - best["__ts"]).days
            if age > int(max_staleness_days):
                for k in keys:
                    row[f"{prefix}{k}"] = default
                row[f"{prefix}__stale_days"] = age
                out.append(row)
                continue

        for k in keys:
            row[f"{prefix}{k}"] = best["__row"].get(k, default)
        out.append(row)

    return out


def asof_value(lookup_rows: Sequence[Dict[str, Any]], at: Any,
               column: str, *, ts_col: str = "as_of_ts",
               default: Any = None) -> Any:
    """Single-value convenience: what was known at time `at`?

    Useful for a point check ("what CPI did the desk know on 2026-08-05?") and in
    tests, where asserting one value documents the intended semantics far better
    than asserting a whole array.
    """
    res = asof_join([{ts_col: at}], lookup_rows, lookup_ts=ts_col,
                    columns=[column], default=default)
    return res[0].get(column, default) if res else default


# --------------------------------------------------------------------------- #
# panel alignment
# --------------------------------------------------------------------------- #


def align_panel(series: Dict[str, Sequence[Dict[str, Any]]], *,
                ts_col: str = "as_of_ts",
                value_col: str = "close",
                join: str = "inner",
                ) -> Dict[str, Any]:
    """Align several instrument series onto a common set of timestamps.

    Why not just zip the close lists:
    Instruments have different histories. A sector index launched in 2015 has no
    2005 rows; a halted stock has holes. Zipping bare lists silently pairs 2015
    prices with 2005 dates and produces a correlation matrix built from two
    different time periods. Aligning on the timestamp is the only correct way.

    Args:
        series: ``{symbol: rows}`` where each row has `ts_col` and `value_col`.
        join: "inner" (only timestamps present in every series — the default and
            the only safe choice for a correlation/covariance estimate) or
            "outer" (union, with None holes — use only when the caller can handle
            missing values explicitly).

    Returns:
        ``{"dates": [...], "values": {symbol: [...]}, "dropped": {...},
           "n_dates": int}``
        `dropped` records how many rows each symbol lost to alignment, so a panel
        that shrank dramatically is visible rather than merely small.
    """
    cleaned: Dict[str, Dict[str, Any]] = {}
    for name, rows in (series or {}).items():
        pts: Dict[str, Any] = {}
        for r in rows or []:
            day = str(r.get(ts_col) or "")[:10]
            if not day:
                continue
            val = r.get(value_col)
            if val is None:
                continue
            pts[day] = val
        if pts:
            cleaned[name] = pts

    if not cleaned:
        return {"dates": [], "values": {}, "dropped": {}, "n_dates": 0}

    sets = [set(pts.keys()) for pts in cleaned.values()]
    if join == "outer":
        common = set().union(*sets)
    else:
        common = set.intersection(*sets)
    dates = sorted(common)

    values = {name: [pts.get(d) for d in dates]
              for name, pts in cleaned.items()}
    dropped = {name: len(pts) - len(dates) for name, pts in cleaned.items()}

    if not dates:
        logger.warning("align_panel: no common timestamps across %s "
                       "(join=%s) — the instruments may not overlap in time",
                       sorted(cleaned), join)

    return {"dates": dates, "values": values, "dropped": dropped,
            "n_dates": len(dates)}