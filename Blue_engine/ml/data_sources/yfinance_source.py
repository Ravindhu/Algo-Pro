"""
ml/data_sources/yfinance_source.py — key-free long-history daily bars for research.

WHY THIS EXISTS (and its one legitimate job)
--------------------------------------------
`Blue_engine/portfolio/providers/yfinance_provider.py` already does the hard
parsing work and documents the traps it works around:

  * yfinance returns a MULTIINDEX even for one ticker (verified on 1.5.x),
  * `^CNXAUTO` returns 404 — Yahoo has no NSE Auto index,
  * `TATAMOTORS.NS` was retired after the demerger; `TMPV.NS` is correct now.

That module's own docstring states its job precisely: **backfill the price cache
so the deep backtest has data**, and it is explicitly "NOT a live price source —
the engine always trades and values on the broker's own feed."

This ML source inherits exactly that scope. It is a *research* source:

    use it for   -> 20+ years of history spanning 2013 taper, 2016 demonetisation,
                    2020 COVID, 2022 rate shock. Regime coverage is the whole
                    point: a model trained only on a bull market is a model that
                    has never seen a drawdown.
    never use it -> as a live signal input, or as the valuation source for the
                    book. Prices there are adjusted differently from the broker's
                    and can disagree by a few basis points — enough to make a
                    backtest that looks tradeable but is not.

A NOTE ON SPLIT/BONUS ADJUSTMENT (a real reconciliation trap)
------------------------------------------------------------
Yahoo's `Close` is adjusted for splits and dividends; the NSE bhavcopy `CLOSE`
is raw. Comparing them directly shows phantom returns on ex-dates. This module
therefore tags every row with `price_basis = "adjusted"` so the feature layer can
refuse to mix bases in one model. Mixing bases is silent — no error, just a model
that has learned a dividend calendar instead of a market.

Optional dependency
-------------------
`yfinance` is optional. Without it, `available()` is False, the registry reports
"present but disabled", and training simply excludes this source. Nothing breaks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import DataSource, FetchResult

__all__ = ["YFinanceSource", "DEFAULT_TICKERS"]


#: Re-exported for convenience so notebooks import one place. The authoritative
#: list lives in the provider module — never copy-and-diverge it.
def _default_tickers() -> Dict[str, str]:
    from ...portfolio.providers import yfinance_provider as yf_mod
    return dict(yf_mod.DEFAULT_TICKERS)


DEFAULT_TICKERS: Dict[str, str] = {}


class YFinanceSource(DataSource):
    """Daily OHLCV from Yahoo, wrapped as ML rows.

    Config keys:
        ticker        Yahoo ticker for the requested symbol, e.g. "^NSEI".
                      When omitted we look the symbol up in the provider's
                      DEFAULT_TICKERS map (NIFTY -> ^NSEI), and fail loudly if it
                      is not there rather than guessing a ticker.
        interval      "1d" (default) or intraday tokens Yahoo supports.
        price_basis   tag stored on every row; default "adjusted".
    """

    name = "yfinance"
    kind = "price"
    required_columns = ("ts", "close", "as_of_ts", "price_basis")

    def available(self) -> bool:
        """True only when the optional `yfinance` package is importable."""
        try:
            from ...portfolio.providers import yfinance_provider as yf_mod
            return bool(yf_mod.available())
        except Exception:  # noqa: BLE001
            return False

    def _ticker_for(self, symbol: str) -> Optional[str]:
        """Resolve a sleeve name to a Yahoo ticker, or None. Never guesses."""
        explicit = self.config.get("ticker")
        if explicit:
            return str(explicit)
        try:
            mapping = _default_tickers()
        except Exception:  # noqa: BLE001
            mapping = {}
        return mapping.get(symbol)

    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult:
        if not self.available():
            self.deg.degraded(
                "fetch", self.name, "dependency_missing",
                "yfinance not installed — long-history backfill unavailable. "
                "Install with: pip install yfinance", symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["yfinance not installed"])

        ticker = self._ticker_for(symbol)
        if not ticker:
            # Do NOT invent a ticker like "SYMBOL.NS" — a wrong ticker returns a
            # plausible-looking series for the wrong instrument, which is far
            # worse than an error. (Yahoo happily serves unrelated tickers.)
            self.deg.fatal(
                "fetch", self.name, "unmapped_symbol",
                f"no Yahoo ticker configured for '{symbol}'. Add it to "
                f"yfinance_provider.DEFAULT_TICKERS or pass config ticker=.",
                symbol=symbol,
                context={"known": sorted(_default_tickers().keys())})
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=[f"unmapped symbol {symbol}"])

        from ...portfolio.providers import yfinance_provider as yf_mod

        res = yf_mod.fetch_history(
            ticker,
            start=start or self.config.get("start", "2005-01-01"),
            end=end,
            interval=str(self.config.get("interval", "1d")),
        )
        for w in res.get("warnings") or []:
            self.deg.degraded("fetch", self.name, "partial", str(w),
                              symbol=symbol)

        if not res.get("ok"):
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=list(res.get("warnings") or []))

        basis = str(self.config.get("price_basis", "adjusted"))
        rows: List[Dict[str, Any]] = []
        for r in res["rows"]:
            bar = str(r.get("ts"))
            rows.append({
                "symbol": symbol,
                "ticker": ticker,
                "ts": bar,
                # Yahoo daily bars are final at the US close, which is ~04:00 IST
                # the next morning. Stamping the IST session close would claim we
                # knew Friday's NSE close from a US source before it existed. We
                # use the bar date + 23:59 IST as a conservative "known by end of
                # that day" marker; the feature layer still shifts by one bar.
                "as_of_ts": f"{bar}T23:59:00+05:30" if "T" not in bar else bar,
                "open": r.get("open"), "high": r.get("high"),
                "low": r.get("low"), "close": r.get("close"),
                "volume": r.get("volume"),
                # Guards against silently mixing adjusted and raw price bases.
                "price_basis": basis,
            })

        return FetchResult(source=self.name, symbol=symbol, ok=True, rows=rows,
                           origin=f"yfinance:{ticker}")

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["ticker"] = self.config.get("ticker")
        d["price_basis"] = self.config.get("price_basis", "adjusted")
        return d