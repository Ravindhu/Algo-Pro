"""
ml/data_sources/openalgo_source.py — broker-fed historical bars via the OpenAlgo SDK.

WHY THIS IS THE AUTHORITY FOR ANYTHING THE DESK ACTUALLY TRADES
---------------------------------------------------------------
`Blue_engine/portfolio/providers/price_cache.py` states the rule this module
exists to serve:

    "the backtest must run on the *same prices you trade on*. A backtest on
     scraped or vendor data that disagrees with your broker's feed is worse than
     no backtest."

Yahoo gives 20 years but a different adjustment basis. NSE bhavcopy gives the
official close but needs manual downloads. **This source gives what the broker
served**, which is the only series the engine can actually execute against.

So the split of responsibilities across the three price sources is deliberate and
worth stating plainly:

    openalgo_source   ground truth for live-traded symbols  (short history, ~1-3y)
    price_cache       the accumulating union of everything fetched  (grows daily)
    yfinance_source   long history for regime research only  (different basis)

A model destined for live use trains on `openalgo` + `price_cache`. A research
model may use `yfinance`, but its rows carry `price_basis="adjusted"` so the
feature layer can refuse to mix bases.

UPGRADE-PROOFING (per AGENTS.md: "openalgo path should be upgrade proof")
------------------------------------------------------------------------
This module talks to the SDK through `client.history(...)` only — the public,
documented call. It never imports OpenAlgo internals, never reads the platform
DB, and never patches the clone. Consequences:

  * `git pull` in `openalgo/` cannot break training,
  * a broker swap changes nothing here (the SDK normalises the response),
  * if the SDK signature ever changes, exactly one function needs updating.

Response-shape handling is delegated to the provider's own verified
`_history_rows`, which already normalises both SDK response shapes (dict-of-lists
and list-of-lists) via `price_cache.normalise_rows`. Reusing it means one place to
fix if a broker changes its payload — no duplication.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base import DataSource, FetchResult

__all__ = ["OpenAlgoSource"]

#: The SDK's daily interval token. Kept as a constant so the one place that knows
#: it is greppable when a broker/SDK change forces a review.
_DAILY = "D"

#: Session close in IST. A daily bar is knowable at that instant (see
#: price_cache.py for the full explanation of why this matters).
_SESSION_CLOSE_IST = "15:30:00+05:30"


class OpenAlgoSource(DataSource):
    """Historical bars from the running OpenAlgo platform.

    Config keys:
        client          an OpenAlgo SDK client (dependency injection; the
                        dashboard/runner already owns one — reusing it avoids a
                        second broker session and a second set of credentials in
                        memory).
        exchange        exchange token, default "NSE". Use "NSE_INDEX" for
                        NIFTY/BANKNIFTY style symbols.
        interval        "D" (default) or an intraday token such as "5minute".
        lookback_days   calendar days to request when no start is given.
                        Default 400: the SDK returns ~250+ trading rows for that
                        window, which is the minimum for a stable covariance or a
                        single walk-forward fold. Requesting more is fine when
                        the broker allows it.
        cache_prices    when True (default), every fetched bar is also upserted
                        into `data/price_cache/prices.db`, so the dataset grows
                        by one day per day with zero extra work. This is what
                        turns a short broker history into a long research set
                        over time.
    """

    name = "openalgo"
    kind = "price"
    required_columns = ("ts", "close", "as_of_ts", "exchange")

    # ---- client resolution ------------------------------------------------ #

    def _client(self) -> Optional[Any]:
        """Return the SDK client, resolving it through the house factory.

        Resolution is delegated to
        `portfolio.providers.factory.get_client()`, which already implements the
        documented precedence (injected -> env hand-off -> built from
        OPENALGO_API_KEY -> None) and *proves* the client is usable with a
        read-only probe before returning it. Re-implementing that here would
        duplicate credential handling — the one thing AGENTS.md says must stay in
        exactly one place.
        """
        injected = self.config.get("client")
        if injected is not None:
            return injected
        try:
            from ...portfolio.providers import factory as factory_mod
            return factory_mod.get_client()
        except Exception as exc:  # noqa: BLE001 - absence is a normal state
            self.log(f"no SDK client available: {exc!r}")
            return None

    def available(self) -> bool:
        return self._client() is not None

    # ---- fetch ------------------------------------------------------------ #

    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult:
        client = self._client()
        if client is None:
            # DEGRADED, not FATAL: with no broker we still have the price cache,
            # so training can legitimately continue from cached bars. The
            # dashboard shows "broker offline — training from cache", which is
            # true and actionable. Calling this FATAL would block a run that is
            # actually fine.
            self.deg.degraded(
                "fetch", self.name, "no_client",
                "no OpenAlgo SDK client (OPENALGO_API_KEY unset or platform "
                "down) — falling back to the price cache for prices",
                symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["no SDK client"])

        exchange = str(self.config.get("exchange", "NSE"))
        interval = str(self.config.get("interval", _DAILY))
        lookback = int(self.config.get("lookback_days", 400))

        end_dt = end or datetime.now().strftime("%Y-%m-%d")
        if start:
            start_dt = start
        else:
            start_dt = (datetime.now()
                        - timedelta(days=lookback)).strftime("%Y-%m-%d")

        # Delegate to the provider's verified normaliser rather than parsing the
        # SDK payload again here. One parser, one place to fix.
        from ...portfolio.providers.openalgo_provider import _history_rows

        rows_raw = _history_rows(client, symbol, exchange,
                                 lookback_days=lookback)
        if not rows_raw:
            # Try the explicit window: `_history_rows` always uses its own
            # lookback, so a caller asking for a specific deep window needs the
            # SDK called directly. We only do that when the helper came back
            # empty, keeping the common path on the verified code.
            rows_raw = self._history_window(client, symbol, exchange,
                                            interval, start_dt, end_dt)

        if not rows_raw:
            self.deg.degraded(
                "fetch", self.name, "empty_window",
                f"SDK returned no bars for {symbol}/{exchange}/{interval} "
                f"[{start_dt} .. {end_dt}]", symbol=symbol,
                context={"exchange": exchange, "interval": interval})
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["no bars from SDK"])

        rows: List[Dict[str, Any]] = []
        for r in rows_raw:
            bar = str(r.get("ts"))
            if start and bar[:10] < start:
                continue
            if end and bar[:10] > end:
                continue
            rows.append({
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "ts": bar,
                # Broker bars are knowable at the NSE close on the bar date.
                "as_of_ts": (bar if "T" in bar
                             else f"{bar}T{_SESSION_CLOSE_IST}"),
                "open": r.get("open"), "high": r.get("high"),
                "low": r.get("low"), "close": r.get("close"),
                "volume": r.get("volume"),
                # Live basis, so the feature layer can tell it apart from Yahoo.
                "price_basis": "broker",
            })

        if not rows:
            self.deg.degraded("fetch", self.name, "empty_window",
                              f"{symbol}: SDK returned {len(rows_raw)} bars but "
                              f"none inside the requested window", symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["no bars in window"])

        # Grow the persistent dataset for free.
        if bool(self.config.get("cache_prices", True)):
            self._persist(symbol, exchange, interval, rows)

        return FetchResult(source=self.name, symbol=symbol, ok=True, rows=rows,
                           origin=f"openalgo:{exchange}/{interval}")

    def _history_window(self, client: Any, symbol: str, exchange: str,
                        interval: str, start: str, end: str
                        ) -> List[Dict[str, Any]]:
        """Call `client.history` for an explicit window. [] on any failure.

        Isolated in its own method because this is the *only* place that knows
        the SDK's `history` keyword names. If the SDK signature changes, this is
        the single function to update — and the tests that exercise it will fail
        loudly rather than the failure surfacing as an empty model.
        """
        from ...portfolio.providers import price_cache as pc_mod

        try:
            resp = client.history(symbol=symbol, exchange=exchange,
                                  interval=interval,
                                  start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001 - SDK/network boundary
            self.log(f"history({symbol}/{exchange}) failed: {exc!r}")
            return []
        if not isinstance(resp, dict) or resp.get("status") != "success":
            return []
        return pc_mod.normalise_rows(resp.get("data") or [])

    def _persist(self, symbol: str, exchange: str, interval: str,
                 rows: List[Dict[str, Any]]) -> None:
        """Upsert fetched bars into the shared price cache. Best-effort.

        Never raises: a disk problem must not abort a data fetch that otherwise
        succeeded. The same rule the ledger and run_history follow.
        """
        try:
            from ...portfolio.providers import price_cache as pc_mod

            cache = pc_mod.PriceCache(self.config.get("cache_db"),
                                      log=self._log_fn)
            n = cache.upsert(symbol, exchange, interval, rows)
            self.log(f"persisted {n} bars for {symbol}/{exchange}/{interval}")
        except Exception as exc:  # noqa: BLE001
            self.deg.degraded("fetch", self.name, "cache_write_failed",
                              f"could not persist {symbol} bars: {exc!r}",
                              symbol=symbol)

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["exchange"] = self.config.get("exchange", "NSE")
        d["interval"] = self.config.get("interval", _DAILY)
        d["has_client"] = self._client() is not None
        return d