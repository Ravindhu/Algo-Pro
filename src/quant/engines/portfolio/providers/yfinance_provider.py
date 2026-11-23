"""
providers/yfinance_provider.py - optional, key-free long-history backfill.

Why this exists
---------------
``client.history()`` gives you as much history as your broker serves — usually
1-3 years of daily candles for indices. That is enough for a walk-forward test,
but not enough to see how a strategy behaves across genuinely different regimes
(2013 taper, 2016 demonetisation, 2020 COVID, 2022 rate shock).

Yahoo Finance carries **20+ years** of NSE index history (``^NSEI`` for NIFTY 50,
``^NSEBANK`` for Bank Nifty, etc.), is free, and needs no API key. So it is the
right tool for one job only: **backfilling the price cache** so the deep
backtest has data. It is NOT a live price source — the engine always trades and
values on the broker's own feed.

Strictly optional
-----------------
``yfinance`` is an optional dependency. If it is not installed, every function
here degrades to ``{"ok": False, "warnings": [...]}`` and the caller continues.
Nothing in the trading path imports this module.

The upgrade-proof rule still holds: this reads a *public* data source over its
own library. It touches neither the OpenAlgo clone nor the broker.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["available", "DEFAULT_TICKERS", "DEFAULT_BASKETS", "fetch_history",
           "fetch_basket", "backfill_cache"]


#: Sleeve -> Yahoo ticker. NSE indices use the ``^`` prefix on Yahoo.
#: Extend freely; the key is your sleeve name, the value is the Yahoo symbol.
#:
#: NOTE on ``AUTO``: Yahoo does NOT carry the NSE Auto index (``^CNXAUTO``
#: returns 404 - verified). Nor is the Nifty Auto ETF old enough (from 2022).
#: So AUTO is served as an **equal-weighted basket of large-cap auto stocks**,
#: declared in :data:`DEFAULT_BASKETS` below. A basket of the majors tracks the
#: sector index closely enough for a *relative* risk/optimiser comparison, which
#: is all this dataset is used for.
DEFAULT_TICKERS: Dict[str, str] = {
    "NIFTY":     "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "IT":        "^CNXIT",
    "PHARMA":    "^CNXPHARMA",
    "METALS":    "^CNXMETAL",
    "ENERGY":    "^CNXENERGY",
    "FMCG":      "^CNXFMCG",
    # REALTY / MIDCAP / AUTO are not on Yahoo; see DEFAULT_BASKETS.
}

#: Sleeves with no direct Yahoo index -> an equal-weighted basket of constituents.
#: The basket series is the mean of the constituents' normalised closes, so it
#: behaves like an equal-weighted sector index.
#:
#: NOTE ``TMPV.NS``: Tata Motors demerged, and Yahoo retired ``TATAMOTORS.NS``
#: (it now returns 404 - verified). ``TMPV.NS`` is the surviving passenger-vehicle
#: entity and is the correct symbol going forward.
DEFAULT_BASKETS: Dict[str, List[str]] = {
    "AUTO": ["MARUTI.NS", "TMPV.NS", "M&M.NS", "BAJAJ-AUTO.NS",
             "HEROMOTOCO.NS", "EICHERMOT.NS", "TVSMOTOR.NS"],
}


def available() -> bool:
    """True when ``yfinance`` is importable. Never raises."""
    try:
        import yfinance  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_history(ticker: str, *, start: Optional[str] = None,
                  end: Optional[str] = None,
                  interval: str = "1d") -> Dict[str, Any]:
    """Fetch one ticker's OHLCV history from Yahoo.

    Returns ``{"ok", "rows", "warnings"}`` where ``rows`` is the canonical
    OHLCV list shape ``price_cache.normalise_rows`` accepts. Degrades to
    ``ok=False`` when ``yfinance`` is missing or the download fails — never
    raises.
    """
    if not available():
        return {"ok": False, "rows": [], "warnings": [
            "yfinance not installed (pip install yfinance) - backfill skipped"]}
    if not ticker:
        return {"ok": False, "rows": [], "warnings": ["no ticker supplied"]}
    try:
        import yfinance as yf
        frame = yf.download(ticker, start=start, end=end, interval=interval,
                            progress=False, auto_adjust=False, threads=False)
    except Exception as exc:  # noqa: BLE001 - network boundary
        return {"ok": False, "rows": [],
                "warnings": [f"yfinance download failed for {ticker}: {exc!r}"]}
    if frame is None or getattr(frame, "empty", True):
        return {"ok": False, "rows": [],
                "warnings": [f"yfinance returned no data for {ticker}"]}

    rows: List[Dict[str, Any]] = []
    try:
        # yfinance returns MULTIINDEX columns -- (Price, Ticker) -- even for a
        # single ticker (verified on yfinance 1.5.x). Flatten to the first level
        # ("Close", "Open", ...) so we can address columns by plain name. We
        # deliberately use positional access below so a duplicate level-0 label
        # (which does happen) cannot raise.
        cols: Dict[str, int] = {}
        for pos, c in enumerate(frame.columns):
            key = str(c[0] if isinstance(c, tuple) else c).strip().lower()
            cols.setdefault(key, pos)

        def _cell(pos: Optional[int], i: int) -> Optional[float]:
            """Read row i of column `pos` as a plain float (None on any issue).

            A cell may come back as a numpy scalar OR a 1-element pandas Series
            (MultiIndex frames do this), so coerce through ``_clean`` which
            handles both.
            """
            if pos is None:
                return None
            try:
                val = frame.iloc[i, pos]
            except Exception:  # noqa: BLE001
                return None
            if hasattr(val, "iloc"):        # a 1-element Series
                try:
                    val = val.iloc[0]
                except Exception:  # noqa: BLE001
                    return None
            return _clean(val)

        for i, ts in enumerate(frame.index):
            rows.append({
                "ts": str(getattr(ts, "date", lambda: ts)()),
                "open": _cell(cols.get("open"), i),
                "high": _cell(cols.get("high"), i),
                "low": _cell(cols.get("low"), i),
                "close": _cell(cols.get("close"), i),
                "volume": _cell(cols.get("volume"), i),
            })
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [],
                "warnings": [f"yfinance frame parse failed for {ticker}: {exc!r}"]}

    rows = [r for r in rows if r.get("close") is not None]
    if not rows:
        return {"ok": False, "rows": [],
                "warnings": [f"no usable rows for {ticker}"]}
    return {"ok": True, "rows": rows, "warnings": []}


def _clean(v: Any) -> Optional[float]:
    """Coerce a pandas/numpy scalar to a plain float (or None)."""
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None      # drop NaN (NaN != NaN)
    except (TypeError, ValueError):
        return None


def fetch_basket(tickers: Sequence[str], *, start: Optional[str] = None,
                 end: Optional[str] = None, interval: str = "1d",
                 weights: Optional[Sequence[float]] = None
                 ) -> Dict[str, Any]:
    """Build an equal-weighted (or given-weight) basket series from constituents.

    Used for sleeves Yahoo has no index for (Nifty Auto). The basket is the
    **weighted mean of each constituent's close normalised to 100 at its first
    observation** - so a constituent that starts later joins at par instead of
    distorting the level, and the result behaves like an equal-weighted sector
    index.

    Returns ``{"ok", "rows", "constituents", "warnings"}`` with ``rows`` in the
    canonical OHLCV shape. Never raises.
    """
    if not tickers:
        return {"ok": False, "rows": [], "constituents": [],
                "warnings": ["no basket tickers supplied"]}
    series: Dict[str, Dict[str, float]] = {}
    warnings: List[str] = []
    for t in tickers:
        res = fetch_history(t, start=start, end=end, interval=interval)
        warnings.extend(res.get("warnings", []))
        if not res.get("ok"):
            continue
        # Normalise to 100 at the first observation so panes are comparable.
        pts = {r["ts"]: r["close"] for r in res["rows"]
               if r.get("close") is not None}
        if not pts:
            continue
        dates = sorted(pts)
        base = pts[dates[0]] or 100.0
        if base <= 0:
            continue
        series[t] = {d: (pts[d] / base * 100.0) for d in dates}

    if not series:
        return {"ok": False, "rows": [], "constituents": [],
                "warnings": warnings + ["no constituent returned data"]}

    # Align on the union of dates; a constituent missing a date is skipped for
    # that date and the remaining weights renormalise, so the basket never
    # forward-fills (which would fabricate zero-return days).
    names = list(series.keys())
    w = (list(weights) if weights and len(weights) == len(names)
         else [1.0 / len(names)] * len(names))
    all_dates = sorted({d for s in series.values() for d in s})
    rows: List[Dict[str, Any]] = []
    for d in all_dates:
        num = 0.0
        den = 0.0
        for i, n in enumerate(names):
            v = series[n].get(d)
            if v is None:
                continue
            num += w[i] * v
            den += w[i]
        if den <= 0:
            continue
        rows.append({"ts": d, "open": None, "high": None, "low": None,
                     "close": round(num / den, 4), "volume": None})
    if not rows:
        return {"ok": False, "rows": [], "constituents": names,
                "warnings": warnings + ["basket produced no aligned rows"]}
    return {"ok": True, "rows": rows, "constituents": names,
            "warnings": warnings}


def backfill_cache(tickers: Optional[Dict[str, str]] = None, *,
                   start: str = "2005-01-01", end: Optional[str] = None,
                   cache: Optional[Any] = None,
                   interval: str = "1d",
                   baskets: Optional[Dict[str, List[str]]] = None,
                   log: Optional[Any] = None) -> Dict[str, Any]:
    """Backfill the price cache from Yahoo for many sleeves.

    Two sources, applied in order:

    1. ``tickers`` - sleeves with a direct Yahoo index/stock symbol.
    2. ``baskets`` - sleeves Yahoo has no index for, built as an equal-weighted
       basket of constituents (defaults to :data:`DEFAULT_BASKETS`, which
       currently covers ``AUTO``).

    ``cache`` is a ``price_cache.PriceCache``; one is created if omitted. The
    Yahoo interval ``"1d"`` is stored under the cache's canonical daily interval
    ``"D"`` so it sits alongside SDK-fetched rows.

    Returns ``{"ok", "written", "sleeves", "warnings"}``.
    """
    if not available():
        return {"ok": False, "written": {}, "sleeves": [], "warnings": [
            "yfinance not installed - backfill skipped"]}
    tickers = dict(tickers or DEFAULT_TICKERS)
    if cache is None:
        from .price_cache import PriceCache
        cache = PriceCache(log=log)
    # Yahoo uses "1d"; the cache's daily interval token is "D".
    cache_interval = "D" if interval in ("1d", "D") else interval

    written: Dict[str, int] = {}
    warnings: List[str] = []
    for sleeve, ticker in tickers.items():
        res = fetch_history(ticker, start=start, end=end, interval=interval)
        warnings.extend(res.get("warnings", []))
        if not res.get("ok"):
            continue
        n = cache.upsert(sleeve, "YAHOO", cache_interval, res["rows"])
        written[sleeve] = n
        if log:
            log(f"[yfinance] {sleeve} ({ticker}): {n} rows backfilled")

    # Sleeves Yahoo has no index for are built from an equal-weighted basket.
    baskets = dict(baskets if baskets is not None else DEFAULT_BASKETS)
    for sleeve, members in baskets.items():
        if sleeve in written:
            continue          # a direct ticker already won
        res = fetch_basket(members, start=start, end=end, interval=interval)
        warnings.extend(res.get("warnings", []))
        if not res.get("ok"):
            warnings.append(f"{sleeve}: basket build failed")
            continue
        n = cache.upsert(sleeve, "YAHOO", cache_interval, res["rows"])
        written[sleeve] = n
        if log:
            log(f"[yfinance] {sleeve} (basket of {len(res['constituents'])}): "
                f"{n} rows backfilled")

    return {"ok": bool(written), "written": written,
            "sleeves": sorted(written.keys()), "warnings": warnings}