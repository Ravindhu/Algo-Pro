"""
backtest/bars.py - the bar-series adapter: cached SQLite rows -> a typed,
validated, look-ahead-safe series the engine can iterate.

WHY AN ADAPTER AND NOT "JUST USE THE DICT LIST"
==============================================
``OptimizerDataCache.load()`` already returns a list of bar dicts, and the
optimizer's objective consumes exactly that. So why not pass the same list
straight to the backtester?

Because the *backtest* has a stricter contract than the *search*. The search
just needs a series to score; the backtest has to be able to PROMOTE a
parameter set into production. That extra responsibility buys three checks the
search does not need, and each one guards a specific way a backtest lies:

1. **Monotonic, unique timestamps.** Out-of-order or duplicate bars silently
   corrupt every rolling computation and can manufacture a look-ahead: a bar
   that appears "later" in the list but is actually "earlier" in time lets the
   strategy see the future. The adapter sorts and de-duplicates, and RECORDS
   what it dropped, rather than trusting the source.

2. **OHLC consistency.** ``low <= min(open, close) <= max(open, close) <= high``.
   A bar that violates this is a data bug (a bad split adjustment, a partial
   bar, a mis-parsed timestamp). Fills computed against an impossible bar are
   fiction. The adapter flags them so the run can be refused rather than
   silently producing a number nobody should act on.

3. **A prefix view with no look-ahead.** The engine hands the strategy
   ``series.view_upto(i)`` - a *read-only* window over bars ``0..i``. The
   strategy physically cannot index bar ``i+1`` through this object. That is the
   structural enforcement of "no look-ahead": it is not a rule the strategy must
   remember, it is an object that makes the violation impossible.

This module is deliberately small. It is a boundary, and a boundary should do
one thing: turn untrusted input into a form the rest of the engine can rely on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import logger

__all__ = ["Bar", "BarSeries", "build_series", "OHLCV_FIELDS"]


OHLCV_FIELDS: Tuple[str, ...] = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Bar:
    """One validated OHLCV bar. Frozen - the engine must not mutate history."""

    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    idx: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "open": self.open, "high": self.high,
                "low": self.low, "close": self.close, "volume": self.volume,
                "idx": self.idx}


@dataclass
class BarSeries:
    """A validated, ordered bar series plus the view API the engine uses.

    Attributes:
        bars: the validated bars, oldest first.
        symbol, exchange, interval: identity, for the report and the store key.
        warnings: every check that failed softly (a dropped duplicate, a bar
            with a zero volume). Surfaced on the result so the operator sees
            what was repaired instead of trusting silently.
        dropped: count of rows removed by the checks.
    """

    bars: List[Bar]
    symbol: str = ""
    exchange: str = ""
    interval: str = "1d"
    warnings: List[str] = field(default_factory=list)
    dropped: int = 0

    def __len__(self) -> int:
        return len(self.bars)

    def __getitem__(self, i: int) -> Bar:
        return self.bars[i]

    @property
    def first_ts(self) -> Optional[str]:
        return self.bars[0].ts if self.bars else None

    @property
    def last_ts(self) -> Optional[str]:
        return self.bars[-1].ts if self.bars else None

    def view_upto(self, i: int) -> "SeriesView":
        """A read-only window over bars ``[0..i]`` inclusive.

        The engine passes this to the strategy. It is the structural guarantee
        against look-ahead: index ``i+1`` simply does not exist on the object.
        """
        end = min(max(0, int(i)) + 1, len(self.bars))
        return SeriesView(self, end)

    def closes(self, start: int = 0, end: Optional[int] = None) -> List[float]:
        stop = len(self.bars) if end is None else min(end, len(self.bars))
        return [b.close for b in self.bars[start:stop]]

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "exchange": self.exchange,
                "interval": self.interval, "n_bars": len(self.bars),
                "first_ts": self.first_ts, "last_ts": self.last_ts,
                "dropped": self.dropped, "warnings": list(self.warnings)}


class SeriesView:
    """Immutable, prefix-only view over a :class:`BarSeries`.

    A strategy receives this, not the list. ``len(view)`` is the number of bars
    available "so far"; ``view[-1]`` is the current bar. There is no way to reach
    a future bar through this object, which is the whole point.
    """

    __slots__ = ("_series", "_n")

    def __init__(self, series: BarSeries, n: int) -> None:
        self._series = series
        self._n = int(n)

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i: int) -> Bar:
        # Negative indexing is allowed but bounded to the prefix, so ``view[-1]``
        # is "the most recent bar" - the idiom every strategy wants.
        if i < 0:
            i = self._n + i
        if i < 0 or i >= self._n:
            raise IndexError(
                f"bar index {i} outside the available prefix [0, {self._n}) - "
                "a strategy must never read a future bar")
        return self._series.bars[i]

    def closes(self) -> List[float]:
        return [b.close for b in self._series.bars[:self._n]]

    def highs(self) -> List[float]:
        return [b.high for b in self._series.bars[:self._n]]

    def lows(self) -> List[float]:
        return [b.low for b in self._series.bars[:self._n]]

    def volumes(self) -> List[float]:
        return [b.volume for b in self._series.bars[:self._n]]

    def to_dict(self) -> Dict[str, Any]:
        return {"n_visible": self._n}


def _to_float(v: Any) -> Optional[float]:
    """Best-effort float. ``None`` for anything non-numeric (never NaN)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Reject NaN / inf: they poison every downstream metric and are never a real
    # market observation. ``f != f`` is the NaN test without importing math.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _ts_key(bar: Dict[str, Any]) -> str:
    """Extract a sortable timestamp string from a bar dict."""
    for k in ("ts", "timestamp", "date", "time", "datetime"):
        v = bar.get(k)
        if v is not None:
            return str(v)
    return ""


def build_series(rows: Iterable[Dict[str, Any]], *,
                 symbol: str = "", exchange: str = "",
                 interval: str = "1d",
                 strict: bool = False) -> BarSeries:
    """Turn cached rows into a validated :class:`BarSeries`.

    Args:
        rows: bar dicts from ``OptimizerDataCache`` (or any source with the same
            keys). Ordered oldest-first is expected but not required - the
            adapter sorts.
        strict: when True, a bar that fails the OHLC consistency check raises
            ``ValueError`` instead of being dropped. Use True for a *promotion*
            backtest (where a bad bar invalidates the conclusion) and False for
            exploratory runs.

    What is dropped, and why that is the honest choice
    --------------------------------------------------
    A row with a missing or non-numeric close cannot be filled against, so it is
    dropped and COUNTED. Silently skipping it would make ``n_bars`` a lie and
    the sample size in the deflated-Sharpe correction wrong. The count and the
    reasons are returned in ``warnings`` so the report can say "of 250 rows, 2
    were unusable" - which is auditable, unlike "we ran on the data".
    """
    warnings: List[str] = []
    by_ts: Dict[str, Bar] = {}
    n_in = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        n_in += 1
        key = _ts_key(raw)
        if not key:
            warnings.append(f"row {n_in}: no timestamp - dropped")
            continue
        vals = {f: _to_float(raw.get(f)) for f in OHLCV_FIELDS}
        # close is the only field we truly require; volume may legitimately be 0
        # on an index or a halted name, and open/high/low may be absent on some
        # providers. We fill from close where a value is genuinely missing so a
        # strategy never sees a None it did not ask for.
        if vals["close"] is None:
            warnings.append(f"row {n_in} ({key}): close missing/non-numeric - dropped")
            continue
        for f in ("open", "high", "low"):
            if vals[f] is None:
                vals[f] = vals["close"]
        if vals["volume"] is None:
            vals["volume"] = 0.0

        lo = min(vals["open"], vals["close"])
        hi = max(vals["open"], vals["close"])
        bad = (vals["low"] > lo or vals["high"] < hi
               or vals["low"] < 0 or vals["close"] <= 0)
        if bad:
            msg = (f"bar {key}: inconsistent OHLC "
                   f"(o={vals['open']} h={vals['high']} "
                   f"l={vals['low']} c={vals['close']})")
            if strict:
                raise ValueError(f"backtest data invalid: {msg}")
            warnings.append(msg + " - dropped")
            continue

        if key in by_ts:
            # A duplicate timestamp is the classic symptom of an upsert that
            # wrote the same bar twice (idempotent writes are fine, but the
            # series must still be unique). Last-write-wins matches the cache's
            # upsert semantics, so the adapter does not invent a different rule.
            warnings.append(f"duplicate bar {key} - kept the later row")
        by_ts[key] = Bar(ts=key, open=vals["open"], high=vals["high"],
                         low=vals["low"], close=vals["close"],
                         volume=vals["volume"], idx=0)

    ordered = [by_ts[k] for k in sorted(by_ts.keys())]
    # Re-index after ordering so ``idx`` is the position in the final series,
    # which is what the engine and the store key on.
    series = BarSeries(
        bars=[Bar(ts=b.ts, open=b.open, high=b.high, low=b.low,
                  close=b.close, volume=b.volume, idx=i)
              for i, b in enumerate(ordered)],
        symbol=symbol, exchange=exchange, interval=interval,
        warnings=warnings, dropped=max(0, n_in - len(ordered)))
    if series.dropped:
        logger.info("build_series: %d of %d rows unusable (%s)",
                    series.dropped, n_in, series.symbol or "?")
    return series
