"""
sectors/providers/vendor_sector.py - sector closes from the pinned vendor repo,
persisted through the project's Data Management Engine, with a live overlay.

WHY THIS EXISTS
---------------
``SectorEngine`` computed Mansfield relative strength from ``sector_market_data``
but **nothing in production ever built that dict** - only ``tests/`` did. Every
real pipeline run therefore returned ``ok=False``, "sector_closes is missing",
and wrote *every* sector into ``wait_and_see_sectors`` with a multiplier of 0.0.
The sector sleeve was silently zeroed, and it looked like a deliberate
defensive decision.

The data was on disk the whole time: the pinned vendor checkout
``Blue_engine/database/vendor/indian-market-historical-data`` ships daily
``data/INDICES/*.parquet`` for NIFTY 50, BANK, IT, PHARMA, MIDCAP 50 and the
broad indices (~2.7 MB, so it stays inside the project).

DATA FLOW (one price database, not two)
---------------------------------------
    vendor parquet
        -> DatabaseEngine.ingest_local_history()
        -> PriceCache  (data/price_cache/prices.db)
        -> read back from the cache
        -> sector_market_data -> SectorEngine

Vendor rows are written under the ``VENDOR_INDEX`` exchange namespace, which is
deliberately distinct from the broker's ``NSE_INDEX``: adjusted Yahoo closes can
never overwrite broker history that the portfolio risk model depends on.

LIVE vs HISTORICAL (stated honestly, never blurred)
---------------------------------------------------
* Default: **historical** vendor daily closes. Classified
  ``historical_vendor`` and warned that it is not live.
* With an OpenAlgo client: today's ``quotes()`` replaces the last row so
  relative strength reflects the current session. Classified
  ``vendor+live_quote``.
* Stale data (older than ``stale_after_days`` calendar days) is used but
  explicitly warned. History is never presented as live market data.

ALIGNMENT IS THE WHOLE JOB
--------------------------
``SectorEngine._matrix`` zips sector and benchmark lists and raises on a length
mismatch, so series are joined on **date**. A late-listed sector, or a day one
index did not trade, would otherwise shift every ratio and manufacture or
destroy relative strength.

NO LOOK-AHEAD
-------------
Every series is truncated at ``as_of`` (default today) before use.

DEGRADE, NEVER HALT
-------------------
Every failure returns ``{"ok": False, "warnings": [...]}``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["VENDOR_DIR", "DEFAULT_INDICES_DIR", "DEFAULT_SECTORS",
           "DEFAULT_BENCHMARK", "VENDOR_EXCHANGE", "VendorSectorProvider",
           "make_provider", "INDEX_TICKERS", "SECTOR_LIVE_SYMBOLS",
           "UNSCORABLE_NSE_SECTORS"]

#: Pinned vendor checkout.
#: Resolved by locating the ``blue_engine`` package folder, never by a fixed
#: ``parents[N]`` depth. The fixed-depth version broke when the engine moved.
def _engine_root() -> Path:
    """The ``blue_engine`` package folder, found by name from this file upward."""
    for parent in Path(__file__).resolve().parents:
        if parent.name == "blue_engine" and (parent / "__init__.py").is_file():
            return parent
    raise RuntimeError("blue_engine package root not found")


ENGINE_DIR = _engine_root()
VENDOR_DIR = (ENGINE_DIR / "database" / "vendor" / "indian-market-historical-data")
DEFAULT_INDICES_DIR = VENDOR_DIR / "data" / "INDICES"

#: Cache exchange namespace for vendor rows (never collides with NSE_INDEX).
VENDOR_EXCHANGE = "VENDOR_INDEX"

#: Vendor file stems -> sleeve names shared with the portfolio universe.
#:
#: COVERAGE: every sleeve in ``portfolio/providers/universe.py:DEFAULT_UNIVERSE``
#: except NIFTY itself (the broad market is the BENCHMARK, not a sector), plus
#: three extra broad sleeves the universe can opt into. Before 2026-10-24 this
#: map held only four sleeves (BANKNIFTY/IT/PHARMA/MIDCAP), so AUTO, METALS,
#: ENERGY, FMCG and REALTY had NO relative-strength reading at all and were
#: silently invisible to the sector allocator.
DEFAULT_SECTORS: Dict[str, str] = {
    "BANKNIFTY": "NIFTY_BANK",
    "IT": "NIFTY_IT",
    "PHARMA": "NIFTY_PHARMA",
    "AUTO": "NIFTY_AUTO",
    "METALS": "NIFTY_METAL",
    "ENERGY": "NIFTY_ENERGY",
    "FMCG": "NIFTY_FMCG",
    "REALTY": "NIFTY_REALTY",
    "FIN_SERVICE": "NIFTY_FIN_SERVICE",
    "PSU_BANK": "NIFTY_PSU_BANK",
    "INFRA": "NIFTY_INFRA",
    "CONSUMER_DURABLES": "NIFTY_CONSUMER_DURABLES",
    "MIDCAP": "NIFTY_MIDCAP_50",
    "MIDCAP_100": "NIFTY_MIDCAP_100",
    "NEXT_50": "NIFTY_NEXT_50",
    "SMALLCAP_250": "NIFTY_SMALLCAP_250",
}

#: OpenAlgo SDK symbol used for the LIVE overlay of each sleeve. The vendor
#: parquet stems (above) are file names, not broker symbols, so this has to be
#: declared rather than derived: ``NIFTY_MIDCAP_50`` is quoted as
#: "NIFTY MIDCAP 50". Sleeves absent here fall back to their own key.
SECTOR_LIVE_SYMBOLS: Dict[str, str] = {
    "BANKNIFTY": "NIFTY BANK",
    "IT": "NIFTY IT",
    "PHARMA": "NIFTY PHARMA",
    "AUTO": "NIFTY AUTO",
    "METALS": "NIFTY METAL",
    "ENERGY": "NIFTY ENERGY",
    "FMCG": "NIFTY FMCG",
    "REALTY": "NIFTY REALTY",
    "FIN_SERVICE": "NIFTY FIN SERVICE",
    "PSU_BANK": "NIFTY PSU BANK",
    "INFRA": "NIFTY INFRA",
    "CONSUMER_DURABLES": "NIFTY CONSUMER DURABLES",
    "MIDCAP": "NIFTY MIDCAP 50",
    "MIDCAP_100": "NIFTY MIDCAP 100",
    "NEXT_50": "NIFTY NEXT 50",
    "SMALLCAP_250": "NIFTY SMALLCAP 250",
}

DEFAULT_BENCHMARK = "NIFTY_50"

#: EVERY index the vendor repo is expected to hold: ``stem -> Yahoo ticker``.
#:
#: This is the single registry the maintenance job reads, because the vendor's
#: upstream updater (``scripts/update_indices.py``) owns its own hard-coded dict
#: and cannot be extended from here. Each ticker below was verified against
#: Yahoo Finance on 2026-10-24 to return at least 250 daily rows ending the
#: latest session:
#:
#:   rows~4700-7200  broad indices since 2004-2011
#:   rows~1200-4100  sectoral/theme indices since 2010-2022
#:   rows= 1165      MIDCAP_100 ("MIDCAP.NS" only lists from 2022-01-28)
#:
#: DELIBERATELY EXCLUDED, because Yahoo has no usable history for them:
#:   NIFTY MEDIA, NIFTY COMMODITIES, NIFTY SERVICES, NIFTY MIDCAP 150 -
#:   each returns a single row (today only), so a parquet would exist but be
#:   useless. Do not add them without a working source; a file that exists and
#:   is empty is worse than a file that is absent, because the alarm below
#:   cannot tell them apart from a real feed.
INDEX_TICKERS: Dict[str, str] = {
    # ---- benchmark + broad market -----
    "NIFTY_50": "^NSEI",
    "SENSEX": "^BSESN",
    "NIFTY_100": "^CNX100",
    "NIFTY_200": "^CNX200",
    "NIFTY_500": "^CRSLDX",
    "NIFTY_NEXT_50": "^NSMIDCP",
    "NIFTY_MIDCAP_50": "^NSEMDCP50",
    "NIFTY_MIDCAP_100": "MIDCAP.NS",
    "NIFTY_MIDCAP_150": "NIFTYMIDCAP150.NS",
    "NIFTY_SMALLCAP_250": "NIFTYSMLCAP250.NS",
    # ---- volatility -----
    "INDIA_VIX": "^INDIAVIX",
    # ---- sectoral -----
    "NIFTY_BANK": "^NSEBANK",
    "NIFTY_IT": "^CNXIT",
    "NIFTY_PHARMA": "^CNXPHARMA",
    "NIFTY_AUTO": "^CNXAUTO",
    "NIFTY_METAL": "^CNXMETAL",
    "NIFTY_ENERGY": "^CNXENERGY",
    "NIFTY_FMCG": "^CNXFMCG",
    "NIFTY_REALTY": "^CNXREALTY",
    "NIFTY_FIN_SERVICE": "NIFTY_FIN_SERVICE.NS",
    "NIFTY_PSU_BANK": "^CNXPSUBANK",
    "NIFTY_INFRA": "^CNXINFRA",
    "NIFTY_CONSUMER_DURABLES": "^CNXCONSUM",
    "NIFTY_MNC": "^CNXMNC",
    "NIFTY_PSE": "^CNXPSE",
}

#: NSE sectoral indices that are IN the portfolio universe but have NO usable
#: price history on any source we can reach. Probed 2026-10-24 against 17 symbol
#: variants (^CNXCEMENT, ^CNXCMT, ^CNXCAPITL, ^CNXCHEM, ^CNXOILGAS,
#: ^CNXHEALTH, .NS and .BO forms): ALL empty, except NIFTY_CEMENT.NS which
#: returns a single row (today only). Yahoo does not carry these NSE sectoral
#: series, so they cannot be scored. This is recorded explicitly rather than
#: silently omitted, so a future data source can fill the gap and the HM score
#: table can state WHICH sectors are unscored and why.
UNSCORABLE_NSE_SECTORS: Dict[str, str] = {
    "NIFTY_CEMENT": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_POWER": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_CAPITAL_GOODS": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_OIL_GAS": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_HEALTHCARE": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_CHEMICALS": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_PRIVATE_BANK": "no price history on Yahoo (all symbol variants empty)",
    "NIFTY_MEDIA": "single row only (today); no usable series",
}


class VendorSectorProvider:
    """Vendor INDICES parquets -> DME/PriceCache -> ``sector_market_data``."""

    name = "vendor_sector"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 client: Any = None):
        self.config = dict(config or {})
        self._log = log
        #: Optional OpenAlgo SDK client. Supplying one enables the live overlay.
        self.client = client if client is not None else self.config.get("client")
        raw_dir = self.config.get("indices_dir")
        self.indices_dir = Path(raw_dir) if raw_dir else DEFAULT_INDICES_DIR
        #: Trailing rows returned. 250 keeps a full year so an advised lookback
        #: can legitimately widen to 50.
        self.lookback_days = int(self.config.get("lookback_days", 250))
        self.benchmark = str(self.config.get("benchmark", DEFAULT_BENCHMARK))
        self.sectors: Dict[str, str] = dict(
            self.config.get("sectors") or DEFAULT_SECTORS)
        self.sectors = {k: v for k, v in self.sectors.items() if v}
        self.price_column = str(self.config.get("price_column", "Adj Close"))
        self.stale_after_days = int(self.config.get("stale_after_days", 5))
        #: SDK symbol for each sleeve when quoting live (defaults to the name).
        self.live_symbols: Dict[str, str] = dict(
            self.config.get("live_symbols") or SECTOR_LIVE_SYMBOLS)
        self._dme = self.config.get("database_engine")
        self._cache_db = self.config.get("cache_db")

    # ---- infrastructure --------------------------------------------------- #
    def log(self, msg: str) -> None:
        if self._log:
            self._log(f"[{self.name}] {msg}")
        else:
            logger.info(msg)

    def _database_engine(self):
        """Lazy DME + shared PriceCache (an injected instance always wins)."""
        if self._dme is None:
            from ...database.engine import DatabaseEngine
            from ...portfolio.providers.price_cache import PriceCache

            cache = PriceCache(self._cache_db) if self._cache_db else PriceCache()
            self._dme = DatabaseEngine(price_cache=cache)
        return self._dme

    # ---- persistence ------------------------------------------------------ #
    def _persist_index(self, name: str, series: Any) -> None:
        """Upsert vendor daily closes into the shared cache via the DME.

        Best-effort: a failed write is logged, and the validated in-memory
        series remains usable. Daily vendor rows keep their YYYY-MM-DD session
        date (no IST reinterpretation).
        """
        rows = [{"timestamp": stamp.date().isoformat(), "close": float(value)}
                for stamp, value in series.items()]
        result = self._database_engine().ingest_local_history(
            symbol=name, exchange=VENDOR_EXCHANGE, interval="D", rows=rows,
            source="vendor:indian-market-historical-data",
            data_class="historical_vendor")
        if not result.get("ok"):
            self.log(f"DME ingest failed for {name}: "
                     f"{(result.get('warnings') or ['unknown'])[0]}")

    def _cache_series(self, name: str, as_of: date) -> Optional[Any]:
        """Date-keyed closes read back from the shared cache."""
        import pandas as pd
        rows = self._database_engine().price_cache.rows(
            name, VENDOR_EXCHANGE, "D")
        if not rows:
            return None
        # VECTORISED parsing. AUDIT 2026-10-24: this used to call
        # pd.to_datetime once per row in a Python loop (~1.3 ms/call on
        # pandas 3.x -> ~6 s per index, ~35 s per fetch). Parsing the whole
        # column at once is ~260x faster with identical errors='coerce'
        # semantics: bad timestamps still become NaT and are dropped.
        stamps = pd.to_datetime(
            pd.Index([row.get("ts") for row in rows]), errors="coerce")
        values = pd.to_numeric(
            pd.Series([row.get("close") for row in rows]), errors="coerce")
        # Same rules as the old loop: real date, positive close, no look-ahead.
        normalised = stamps.normalize()
        keep = stamps.notna() & values.notna() & (values > 0)
        keep = keep & (normalised <= pd.Timestamp(as_of))
        normalised, values = normalised[keep], values[keep]
        if len(normalised) == 0:
            return None
        series = pd.Series(values.to_numpy(), index=normalised)
        # Last write wins for a duplicated session date, as before.
        series = series[~series.index.duplicated(keep="last")].sort_index()
        return series if len(series) else None

    def _source_series(self, name: str, series: Any, as_of: date) -> Any:
        """Persist the vendor series, then serve it back from the cache.

        The read-back is the point: from here the sector engine consumes the
        same DB every other consumer uses. If the cache is unavailable we fall
        back to the freshly-read vendor series, so a broken cache degrades the
        persistence rather than the data.
        """
        self._persist_index(name, series)
        cached = self._cache_series(name, as_of)
        if cached is not None and len(cached) >= int(len(series) * 0.9):
            return cached
        self.log(f"cache read-back short for {name}; using the vendor series")
        return series

    # ---- live overlay ------------------------------------------------------ #
    def _live_quote(self, symbol: str) -> Optional[tuple]:
        """Latest (price, date) for one index via the SDK ``quotes()``."""
        if self.client is None:
            return None
        try:
            resp = self.client.quotes(symbol=symbol, exchange="NSE_INDEX")
        except Exception as exc:  # noqa: BLE001 - SDK call boundary
            self.log(f"quotes failed for {symbol}: {type(exc).__name__}")
            return None
        if not isinstance(resp, dict) or resp.get("status") != "success":
            return None
        data = resp.get("data") or {}
        raw = data.get("ltp", data.get("last_price", data.get("last")))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return (value, date.today()) if value > 0 else None

    def _apply_live(self, series: Any, symbol: str) -> tuple:
        """Replace the last row with today's quote. Returns (series, used)."""
        import pandas as pd

        quote = self._live_quote(symbol)
        if quote is None:
            return series, False
        price, live_date = quote
        frame = pd.Series(series)
        frame = frame[frame.index.date < live_date]
        frame.loc[pd.Timestamp(live_date)] = price
        return frame.sort_index(), True

    # ---- vendor reading ---------------------------------------------------- #
    def _read_index(self, stem: str, as_of: date) -> Optional[Any]:
        """Per-date close series for one vendor file, truncated at ``as_of``."""
        path = self.indices_dir / f"{stem}.parquet"
        if not path.is_file():
            self.log(f"vendor index missing: {path.name}")
            return None
        try:
            import pandas as pd  # optional dependency
        except Exception:  # noqa: BLE001
            self.log("pandas not installed; vendor sector data skipped")
            return None
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            self.log(f"vendor read failed ({path.name}): {exc!r}")
            return None
        if frame.empty:
            return None

        column = self.price_column
        if column not in frame.columns:
            column = "Close" if "Close" in frame.columns else ""
        if not column:
            self.log(f"{path.name} has no Close/Adj Close column")
            return None

        try:
            if "Date" in frame.columns:
                idx = pd.to_datetime(frame["Date"], errors="coerce")
            else:
                idx = pd.to_datetime(frame.index, errors="coerce")
        except Exception as exc:  # noqa: BLE001
            self.log(f"{path.name}: unparseable dates ({exc!r})")
            return None

        series = frame[column].astype("float64")
        series.index = idx
        series = series[~series.index.isna()].dropna()
        # NO LOOK-AHEAD: never use a row dated after `as_of`.
        series = series[series.index.date <= as_of]
        series = series[~series.index.duplicated(keep="last")].sort_index()
        return series if len(series) else None

    # ---- provider contract ------------------------------------------------- #
    def fetch(self) -> Dict[str, Any]:
        """Return the ``sector_market_data`` dict, or ``{"ok": False, ...}``."""
        warnings: List[str] = []
        if not self.indices_dir.is_dir():
            return {"ok": False, "warnings": [
                f"vendor indices dir missing ({self.indices_dir})"]}

        as_of_cfg = self.config.get("as_of")
        as_of_date = (datetime.fromisoformat(str(as_of_cfg)).date()
                      if as_of_cfg else date.today())

        try:
            import pandas as pd
        except Exception:  # noqa: BLE001
            return {"ok": False, "warnings": [
                "pandas not installed; vendor sector data unavailable"]}

        benchmark = self._read_index(self.benchmark, as_of_date)
        if benchmark is None:
            return {"ok": False, "warnings": [
                f"benchmark {self.benchmark} unreadable; cannot compute RS"]}
        benchmark = self._source_series(self.benchmark, benchmark, as_of_date)

        series: Dict[str, Any] = {}
        skipped: List[str] = []
        for sector_name, stem in self.sectors.items():
            found = self._read_index(stem, as_of_date)
            if found is None:
                skipped.append(sector_name)
                continue
            series[sector_name] = self._source_series(stem, found, as_of_date)
        if skipped:
            warnings.append(
                f"sectors unavailable and skipped: {', '.join(skipped)}")
        if not series:
            return {"ok": False, "warnings": warnings + [
                "no sector series could be read"]}

        # ---- live overlay (optional, honest labelling) ------------------- #
        live_used = False
        if self.client is not None:
            benchmark, used = self._apply_live(benchmark,
                                              self.live_symbols.get(
                                                  "BENCHMARK", "NIFTY 50"))
            live_used = live_used or used
            for sector_name in list(series):
                series[sector_name], used = self._apply_live(
                    series[sector_name],
                    self.live_symbols.get(sector_name, sector_name))
                live_used = live_used or used
            if live_used:
                warnings.append(
                    "last row replaced with a live SDK quote; history remains "
                    "vendor-sourced")
            else:
                warnings.append(
                    "live quotes unavailable; using historical vendor closes")

        classification = "vendor+live_quote" if live_used else "historical_vendor"
        if not live_used:
            warnings.append(
                "data is historical vendor daily closes, not live market data")

        # Date-aligned join: the engine zips the lists, so a length mismatch is
        # a hard error there. Aligning here makes that impossible.
        frame = pd.DataFrame({"__bench__": pd.Series(benchmark),
                              **{k: pd.Series(v) for k, v in series.items()}})
        frame = frame.dropna()
        if len(frame) < 20:
            return {"ok": False, "warnings": warnings + [
                f"only {len(frame)} aligned rows; need at least 20"]}
        frame = frame.tail(self.lookback_days)

        bench_closes = [float(v) for v in frame["__bench__"].tolist()]
        sector_closes = {name: [float(v) for v in frame[name].tolist()]
                         for name in series}

        latest = frame.index.max().date()
        as_of_str = latest.isoformat()
        age_days = (as_of_date - latest).days
        if age_days > self.stale_after_days:
            warnings.append(
                f"vendor index data is stale: latest {as_of_str}, {age_days} "
                f"calendar days old (threshold {self.stale_after_days})")

        # A flat series scores 0 forever and looks stable.
        stale = [name for name, closes in sector_closes.items()
                 if len(set(closes[-10:])) == 1]
        if stale:
            warnings.append(
                f"possible stale/forward-filled series: {', '.join(stale)} "
                f"(last 10 closes identical)")

        self.log(f"aligned {len(sector_closes)} sectors x {len(bench_closes)} "
                 f"rows through {as_of_str} ({classification})")

        return {
            "ok": True,
            "benchmark_name": self.benchmark,
            "benchmark_closes": bench_closes,
            "sector_closes": sector_closes,
            "data_classification": classification,
            "as_of": as_of_str,
            "source": "vendor:indian-market-historical-data/shared-price-cache",
            "warnings": warnings,
        }

    #: ``provider_ref = "blue_engine.sectors.providers.vendor_sector:make_provider"``
    def __call__(self) -> Dict[str, Any]:
        return self.fetch()


def make_provider(config: Optional[Dict[str, Any]] = None
                  ) -> Callable[[], Dict[str, Any]]:
    """Factory for ``engines.sector.provider_ref``."""
    return VendorSectorProvider(config)
