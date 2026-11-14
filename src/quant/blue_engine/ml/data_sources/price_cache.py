"""
ml/data_sources/price_cache.py — adapt Blue_engine's own OHLCV cache into a
DataSource.

WHY THIS IS THE FIRST SOURCE AND THE DEFAULT ONE
------------------------------------------------
`Blue_engine/portfolio/providers/price_cache.py` already exists and already
accumulates a persistent SQLite OHLCV store at `data/price_cache/prices.db`.
It has a property nothing else here has: **it is the same prices the engine
trades on**. Per that module's own docstring: "the backtest must run on the
same prices you trade on. A backtest on scraped or vendor data that disagrees
with your broker's feed is worse than no backtest."

So the ML layer does not fetch prices at all. It *reads what we already own*.
That gives us three things for free:

  1. **No new network dependency** — training works on a plane, offline.
  2. **No new credentials** — nothing to leak.
  3. **No disagreement** between the model's view of price and the desk's.

THE as_of_ts CONVENTION FOR DAILY BARS (read this before changing anything)
---------------------------------------------------------------------------
A daily bar stamped `2026-09-25` (Friday) is *knowable* at the end of that
session. We therefore set:

    as_of_ts = <bar date> + 15:30 IST   ->  "2026-09-25T15:30:00+05:30"

and then, in the feature builder, every feature for a prediction made at the
open of the next session is shifted by one bar. The combination of
(end-of-bar timestamp) + (one-bar shift) is what makes the pipeline leak-free.
Stamping a bar with its *open* time would silently hand the model the close it
is supposed to predict — the single most common self-inflicted quant ML bug.

If you are reading this because a backtest suddenly looks too good, check here
first.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import DataSource, FetchResult

__all__ = ["PriceCacheSource"]

#: NSE closes at 15:30 IST. A daily bar becomes knowable at that moment.
_SESSION_CLOSE_IST = "15:30:00+05:30"


def _as_of_from_bar_date(bar_date: str) -> str:
    """Turn a cache `ts` value into an ISO knowledge timestamp.

    The cache stores dates as plain `YYYY-MM-DD` (and intraday as
    `YYYY-MM-DD HH:MM:SS`). We normalise both, and *never* invent a date: if the
    input is unparseable we return it unchanged so the quality gate can flag it
    rather than us silently fabricating a timestamp.
    """
    ts = str(bar_date).strip()
    if not ts:
        return ts
    if "T" in ts or "+" in ts:
        return ts                       # already an aware ISO timestamp
    if " " in ts:                       # "2026-09-25 14:15:00" -> intraday bar
        date_part, _, time_part = ts.partition(" ")
        return f"{date_part}T{time_part}+05:30"
    return f"{ts}T{_SESSION_CLOSE_IST}"  # plain daily bar


class PriceCacheSource(DataSource):
    """Read OHLCV rows for one instrument out of `data/price_cache/prices.db`.

    Configuration keys (all optional):
        cache_db   path to the SQLite file; default is the engine's own
                   `data/price_cache/prices.db` so the two never diverge.
        exchange   exchange token stored in the cache. Default `NSE_INDEX`
                   because the sleeve universe is index-based; pass `NSE` for
                   single-name equities or `YAHOO` for backfilled sleeves.
        interval   cache interval token: `D` (default), `5minute`, `15minute`.
    """

    name = "price_cache"
    kind = "price"
    required_columns = ("ts", "close", "as_of_ts")

    # ---- construction ----------------------------------------------------- #

    def _cache(self):
        """Build (once) a PriceCache bound to the configured DB path.

        Imported lazily so this module can be imported — and unit-tested with a
        fake cache — in an environment where the portfolio package is absent.
        """
        if getattr(self, "_cache_obj", None) is None:
            from ...portfolio.providers import price_cache as pc_mod

            self._cache_obj = pc_mod.PriceCache(
                self.config.get("cache_db"), log=self._log_fn)
        return self._cache_obj

    def available(self) -> bool:
        """True when the DB exists and holds at least one row."""
        try:
            return bool(self._cache().available())
        except Exception:  # noqa: BLE001 - availability probe must not raise
            return False

    # ---- fetch ------------------------------------------------------------ #

    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult:
        exchange = str(self.config.get("exchange", "NSE_INDEX"))
        interval = str(self.config.get("interval", "D"))

        cache = self._cache()
        if not cache.available():
            # FATAL, not DEGRADED: with no prices there is nothing to learn from.
            # Marking this DEGRADED would let a caller override it and train on
            # an empty frame — the exact "guess" AGENTS.md forbids.
            self.deg.fatal("fetch", self.name, "cache_empty",
                           f"price cache has no rows at {cache.db_path}",
                           symbol=symbol,
                           context={"db": str(cache.db_path)})
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["price cache empty"],
                               origin=str(cache.db_path))

        rows = cache.rows(symbol, exchange, interval)
        if not rows:
            # Try the Yahoo-backfilled copy: `backfill_cache` stores the same
            # sleeves under exchange "YAHOO". This is a legitimate second chance,
            # not a silent substitution, so we record it as INFO and keep going.
            alt = cache.rows(symbol, "YAHOO", interval)
            if alt:
                self.deg.info("fetch", self.name, "exchange_fallback",
                              f"{symbol}: no rows on {exchange}; used YAHOO backfill",
                              symbol=symbol)
                rows, exchange = alt, "YAHOO"

        if not rows:
            self.deg.degraded("fetch", self.name, "symbol_absent",
                              f"{symbol} not present in the price cache "
                              f"({exchange}/{interval})", symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=[f"{symbol} absent from price cache"])

        # Window filter (inclusive) on the bar date, before we add as_of_ts.
        def _in_window(d: str) -> bool:
            day = str(d)[:10]
            if start and day < start:
                return False
            if end and day > end:
                return False
            return True

        out: List[Dict[str, Any]] = []
        for r in rows:
            if not _in_window(r.get("ts", "")):
                continue
            out.append({
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "ts": r.get("ts"),
                # ---- the leak barrier ----
                "as_of_ts": _as_of_from_bar_date(r.get("ts")),
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "volume": r.get("volume"),
            })

        if not out:
            self.deg.degraded("fetch", self.name, "empty_window",
                              f"{symbol}: cache has {len(rows)} rows but none in "
                              f"[{start or '*'} .. {end or '*'}]", symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["no rows in requested window"])

        # Coverage goes into `origin` (a human-readable provenance string) and
        # NOT into the rows, so `FetchResult.rows` stays a clean hashable table.
        # The quality gate re-derives counts from the rows themselves — the
        # authoritative source — rather than trusting a second-hand number.
        coverage = cache.coverage(symbol, exchange, interval)
        res = FetchResult(source=self.name, symbol=symbol, ok=True, rows=out)
        res.origin = (f"price_cache:{exchange}/{interval} "
                      f"n={coverage.get('n')} "
                      f"[{coverage.get('first')} .. {coverage.get('last')}]")
        return res

    # ---- introspection ---------------------------------------------------- #

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["db"] = str(self.config.get("cache_db") or "default (engine price cache)")
        return d