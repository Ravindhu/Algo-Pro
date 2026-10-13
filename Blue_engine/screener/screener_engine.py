"""
screener_engine.py - the Screener engine (fundamental universe selection).

WHERE THIS FITS IN THE PIPELINE
-------------------------------
    Economic -> Market -> Portfolio -> Alpha -> POV -> Infrastructure
                                       ^
                                       |
                    Screener engine feeds the Alpha stage

The Alpha engine (``alpha/alpha_engine.py``) already takes a ``fundamentals``
dict per symbol and scores it. What it does NOT do is *decide which symbols
exist* - it screens whatever universe it is handed. This engine is that missing
half: it goes to screener.in, applies the fundamental rules, and emits the
candidate universe with the exact metric keys Alpha already consumes
(``pe``, ``roe``, ``de``, ...) plus the two the request named
(``market_cap``, ``debt_equity``).

So the wiring is:

    ScreenerEngine.screen()  ->  {"symbols": [...], "fundamentals": {...}}
                             ->  AlphaEngine  ->  POV

The Screener engine declares ``EngineStage.MARKET``... no. Read on.

STAGE CHOICE
------------
It is registered at ``EngineStage.PORTFOLIO`` - *before* Alpha, because the
universe must exist before names can be scored or sized. It is deliberately NOT
inserted into ``MacroPipeline``'s default engine list: ``MacroPipeline`` is a
pure, offline, deterministic chain, and bolting an HTTP scraper into it would
break that contract for every existing test and every existing run. Instead this
engine is (a) runnable standalone, and (b) consumable as a *provider* by the
Alpha stage, which is how the pipeline stays pure and the scraper stays optional.

DATA-DATE DISCIPLINE
--------------------
Every result carries ``as_of`` and ``source`` fields. screener.in shows the last
reported *quarter*, not today's price. A market cap derived from it is therefore
a point-in-time fundamental, NOT a live valuation. ``MarketEngine``/OpenAlgo
``quotes()`` own live prices. The engine never mixes the two, and the dashboard
renders the data date next to every screen result.

SAFETY
------
* Read-only against screener.in. No orders, no OpenAlgo import, no config writes.
* Never raises: any failure degrades to an ``EngineResult`` with ``ok=False``.
* Bounded: ``max_symbols`` caps the sweep, ``min_interval_ms`` caps the request
  rate, and ``max_pages`` caps the pagination walk. See ``polite_limits()``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from .client.numbers import (
    latest_period_value,
    ratio_lookup,
    table_row_lookup,
    to_number,
)
from .client.screener_client import ScreenerClient
from .filters import Screener

logger = logging.getLogger(__name__)

#: Default cache/state location, sibling to the engine. Sits next to the engine
#: so a fresh clone has a working cache dir without any setup.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache"


class ScreenerEngine(BaseEngine):
    """Fundamental screening engine backed by the vendored screener-india port.

    Config block (``screener`` in ``screener_engine.json``)::

        {
          "enabled": true,
          "source": "sector",              # "sector" | "symbols" | "screen"
          "sectors": ["IN02/IN0201"],      # sector slugs for source="sector"
          "screen_id": null, "screen_slug": null,
          "symbols": ["RELIANCE", "TCS"],  # for source="symbols"
          "max_symbols": 250,
          "min_interval_ms": 400,
          "cache_ttl_ms": 21600000,
          "cache_dir": null,               # null -> blue_engine/screener/cache
          "fetch_timeout_ms": 20000,
          "max_retries": 2,
          "cookies": "",
          "proxy_url": ""
        }
    """

    name = "screener"
    #: Runs before Alpha so the universe exists before scoring.
    stage = EngineStage.SCREENER

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 client: Optional[ScreenerClient] = None,
                 **kwargs: Any):
        # `client` is a test/reuse seam: inject a client pointed at a fixture
        # server and the engine needs no network at all.
        super().__init__(config, provider, kwargs.get("log"))
        self._client_override = client
        self._client: Optional[ScreenerClient] = None

        # Screen definition lives in its own sub-block so a screen can be
        # swapped without touching transport settings.
        self.screen = Screener.from_config(self.config.get("screen") or {})

        self.source = str(self.config.get("source") or "sector").lower()
        self.sectors = list(self.config.get("sectors") or [])
        self.symbols = [str(s).upper() for s in (self.config.get("symbols") or [])]
        self.screen_id = self.config.get("screen_id")
        self.screen_slug = self.config.get("screen_slug")

        self.max_symbols = int(self.config.get("max_symbols", 250))
        self.max_pages = int(self.config.get("max_pages", 40))
        self.include_all_pages = bool(self.config.get("include_all_pages", True))
        #: Enrich each candidate with its own company page (per-symbol fetch).
        #: Off by default: the sector/screen table already carries the columns we
        #: need, so enrichment multiplies the request count by N for little gain.
        #: Turn it on when a metric is missing from the table (e.g. debt/equity).
        self.enrich = bool(self.config.get("enrich", False))
        self.enrich_limit = int(self.config.get("enrich_limit", self.max_symbols))

    # ------------------------------------------------------------------ #
    # Client
    # ------------------------------------------------------------------ #
    def _get_client(self) -> ScreenerClient:
        """Build (once) the transport client from config."""
        if self._client_override is not None:
            return self._client_override
        if self._client is None:
            cache_dir = self.config.get("cache_dir")
            if cache_dir is None:
                cache_dir = str(DEFAULT_CACHE_DIR)
            self._client = ScreenerClient(
                base_url=str(self.config.get("base_url") or "https://www.screener.in"),
                cache_ttl_ms=int(self.config.get("cache_ttl_ms", 21_600_000)),
                min_interval_ms=int(self.config.get("min_interval_ms", 400)),
                max_retries=int(self.config.get("max_retries", 2)),
                timeout_ms=int(self.config.get("fetch_timeout_ms", 20_000)),
                proxy_url=str(self.config.get("proxy_url") or ""),
                cookies=str(self.config.get("cookies") or ""),
                cache_dir=cache_dir,
                verify_tls=bool(self.config.get("verify_tls", True)),
            )
        return self._client

    # ------------------------------------------------------------------ #
    # Polite limits (surfaced in the UI so the operator sees the policy)
    # ------------------------------------------------------------------ #
    def polite_limits(self) -> Dict[str, Any]:
        return {
            "min_interval_ms": int(self.config.get("min_interval_ms", 400)),
            "max_symbols": self.max_symbols,
            "max_pages": self.max_pages,
            "cache_ttl_ms": int(self.config.get("cache_ttl_ms", 21_600_000)),
            "note": ("unofficial scraper of a public site; rate is capped by "
                     "design and raised only deliberately"),
        }

    # ------------------------------------------------------------------ #
    # Universe acquisition
    # ------------------------------------------------------------------ #
    def _from_sectors(self, client: ScreenerClient) -> tuple:
        """Build the candidate list from one or more sector tables."""
        candidates: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for sector in self.sectors:
            res = client.get_sector_data(
                sector, limit=50, include_all_pages=self.include_all_pages,
                max_pages=self.max_pages)
            warnings.extend(res.warnings)
            if not res.ok or not res.data:
                warnings.append(f"sector {sector!r} failed: {res.error}")
                continue
            for row in res.data.get("rows") or []:
                cand = self._candidate_from_row(row, sector=sector)
                if cand:
                    candidates.append(cand)
            if len(candidates) >= self.max_symbols:
                warnings.append(
                    f"candidate cap of {self.max_symbols} reached during sectors "
                    f"(remaining sectors not fetched)")
                break
        return candidates, warnings

    def _from_screen(self, client: ScreenerClient) -> tuple:
        """Build the candidate list from a saved public screener.in screen."""
        warnings: List[str] = []
        if not self.screen_id or not self.screen_slug:
            return [], ["source='screen' requires screen_id and screen_slug"]
        res = client.get_screen_details(
            int(self.screen_id), str(self.screen_slug),
            include_all_pages=self.include_all_pages, max_pages=self.max_pages)
        warnings.extend(res.warnings)
        if not res.ok or not res.data:
            return [], [f"screen {self.screen_id} failed: {res.error}"]
        candidates = []
        for row in res.data.get("rows") or []:
            cand = self._candidate_from_row(row)
            if cand:
                candidates.append(cand)
        return candidates, warnings

    def _from_symbols(self, client: ScreenerClient) -> tuple:
        """Build candidates from an explicit symbol list (one fetch per symbol)."""
        candidates: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for symbol in self.symbols[: self.max_symbols]:
            cand = self._candidate_from_company(client, symbol)
            if cand is None:
                warnings.append(f"{symbol}: fetch or parse failed")
                continue
            candidates.append(cand)
        return candidates, warnings

    def _candidate_from_company(self, client: ScreenerClient,
                                symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch one company page and extract the metric dict."""
        res = client.get_company(symbol, "consolidated")
        if not res.ok or not res.data:
            self.log(f"{symbol}: company fetch failed ({res.error})")
            return None
        return {
            "symbol": symbol,
            "name": res.data.get("name") or "",
            "sector": None,
            "metrics": self._metrics_from_company(res.data),
        }

    def _candidate_from_row(self, row: Dict[str, Any],
                            sector: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Extract a candidate from a sector/screen table row.

        Column headers on those tables drift ("Name" vs "Company", "Mar Cap" vs
        "Market Cap"). We therefore match by a small set of accepted keys rather
        than by position, and we return ``None`` when no symbol column exists at
        all, because a row we cannot identify must not become a blank symbol.
        """
        if not isinstance(row, dict):
            return None

        symbol = _first_present(row, ("Name", "Symbol", "Company", "Ticker"))
        if not symbol:
            return None
        symbol = str(symbol).strip().upper()
        # A row can carry a link label like "RELIANCE 1" - keep the first token.
        symbol = symbol.split()[0] if symbol else ""
        if not symbol or len(symbol) > 20:
            return None

        raw = {
            "market_cap": _first_present(row, ("Mar Cap", "Market Cap", "Mkt Cap",
                                               "MarketCap")),
            "current_price": _first_present(row, ("CMP", "Current Price", "Price",
                                                  "LTP")),
            "pe": _first_present(row, ("P/E", "Stock P/E", "PE")),
            "roe": _first_present(row, ("ROE", "ROE %", "ROE%")),
            "roce": _first_present(row, ("ROCE", "ROCE %", "ROCE%")),
            "debt_equity": _first_present(row, ("Debt/Equity", "Debt to equity",
                                                "Debt Equity", "D/E")),
            "dividend_yield": _first_present(row, ("Div Yld", "Dividend Yield",
                                                    "Div Yield", "Div Yld %")),
            "sales": _first_present(row, ("Sales", "Revenue")),
            "net_profit": _first_present(row, ("Net Profit", "NP", "PAT")),
        }
        # Drop nothing here: the screen's UNKNOWN policy decides what a missing
        # metric means. If we filtered None out now, a "-" and an absent column
        # would become indistinguishable from a real 0 downstream.
        return {"symbol": symbol, "name": symbol, "sector": sector, "metrics": raw}

    def _metrics_from_company(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract the metric dict from a full company snapshot (top ratios).

        This is the authoritative path. ``debt_equity`` is not always a top
        This is the authoritative path. screener.in does NOT publish a
        "Debt to equity" cell on the public company page (verified against a
        live RELIANCE page: the ratios table carries Debtor Days, Inventory
        Days and ROCE, but no debt/equity). So ``debt_equity`` is DERIVED from
        the balance sheet instead - see :meth:`_debt_equity_from_balance_sheet`.
        """
        tr = data.get("top_ratios") or []
        metrics: Dict[str, Any] = {
            "market_cap": ratio_lookup(tr, "market_cap"),
            "current_price": ratio_lookup(tr, "current_price"),
            "pe": ratio_lookup(tr, "pe"),
            "roe": ratio_lookup(tr, "roe"),
            "roce": ratio_lookup(tr, "roce"),
            "dividend_yield": ratio_lookup(tr, "dividend_yield"),
            "book_value": ratio_lookup(tr, "book_value"),
        }

        # 1st choice: an explicit debt/equity cell, when the page happens to
        # carry one (some pages/plans do).
        de = ratio_lookup(tr, "debt_equity")
        if de is None:
            table = data.get("ratios")
            row = table_row_lookup(table, ("debt to equity", "debt/equity"))
            if row is not None:
                de = latest_period_value(row, headers=(table or {}).get("headers"))

        # 2nd choice: derive it from the balance sheet. This is the normal path.
        if de is None:
            de = self._debt_equity_from_balance_sheet(data.get("balance_sheet"))

        metrics["debt_equity"] = de
        # Record where the number came from. "reported" vs "derived" is the
        # difference between a figure you can reconcile with a vendor and one
        # you cannot, and a rejection reason should say which it was.
        metrics["debt_equity_source"] = (
            "reported" if de is not None else "unavailable")
        return metrics

    def _debt_equity_from_balance_sheet(self,
                                        table: Optional[Dict[str, Any]]) -> Optional[float]:
        """Derive debt/equity from the balance sheet's latest period.

        screener.in's balance sheet reports (label -> meaning):

            Equity Capital     share capital
            Reserves           retained earnings + other reserves
            Borrowings +       total debt  <-- the numerator

        so

            debt/equity = Borrowings / (Equity Capital + Reserves)

        We return ``None`` - never 0 - when any of the three inputs is missing
        or when equity is <= 0 (a negative-equity company has a meaningless
        ratio; treating it as 0 would let a distressed name pass "< 1").

        This mirrors the widely used definition (total debt over shareholders'
        equity). It is NOT identical to every vendor's number: some include
        lease liabilities or use standalone vs consolidated figures. The engine
        labels the metric's provenance so the difference is visible rather than
        hidden - see ``metrics_derived`` in the engine output.
        """
        if not table:
            return None
        headers = table.get("headers") or []
        if not headers:
            return None

        # NOTE on the label: screener.in uses "Borrowings +" for most companies
        # but "Borrowing" (singular, no '+') for banks - verified live against
        # HDFCBANK vs ITC. Matching the STEM "borrowing" covers "Borrowing",
        # "Borrowings" and "Borrowings +". Listing both spellings separately
        # was the bug: a substring match on "borrowings" MISSES "borrowing"
        # only if the row is compared for equality, and misses nothing if it is
        # compared as a substring - but relying on that coincidence is fragile,
        # so the stem is used explicitly.
        borrowings = latest_period_value(
            table_row_lookup(table, ("borrowing", "total debt", "debt")),
            headers=headers)
        equity_capital = latest_period_value(
            table_row_lookup(table, ("equity capital", "share capital")),
            headers=headers)
        reserves = latest_period_value(
            table_row_lookup(table, ("reserves",)),
            headers=headers)

        if borrowings is None or equity_capital is None or reserves is None:
            return None
        equity = equity_capital + reserves
        if equity <= 0:
            # Negative or zero book equity: the ratio is not meaningful, and a
            # small positive number here would wrongly satisfy a "< 1" filter.
            return None
        return borrowings / equity

    def _metrics_derived(self) -> Dict[str, str]:
        """Which metrics are computed rather than scraped verbatim.

        Surfaced in the engine output so a consumer (and the dashboard) can tell
        a reported figure from a derived one. Fundamental data provenance is not
        optional in a trading system - the same number from two vendors differs
        and the difference is the first question you ask when a screen
        disagrees with your broker.
        """
        return {
            "debt_equity": (
                "derived: Borrowings / (Equity Capital + Reserves), "
                "latest balance-sheet period"),
        }

    # ------------------------------------------------------------------ #
    # Enrichment (opt-in per-symbol fetch)
    # ------------------------------------------------------------------ #
    def _enrich(self, client: ScreenerClient,
                candidates: List[Dict[str, Any]]) -> List[str]:
        """Fill missing metrics from each candidate's own company page.

        Only runs when ``enrich`` is true. Only the FIRST ``enrich_limit``
        candidates are enriched, and only metrics that are currently missing are
        touched - an existing table value is never overwritten, because the
        table value and the company page can legitimately differ (consolidated
        vs standalone) and silently swapping one for the other would make a
        screen non-reproducible.
        """
        warnings: List[str] = []
        for cand in candidates[: self.enrich_limit]:
            missing = [k for k, v in (cand.get("metrics") or {}).items()
                       if to_number(v) is None]
            if not missing:
                continue
            res = client.get_company(cand["symbol"], "consolidated")
            if not res.ok or not res.data:
                warnings.append(f"enrich {cand['symbol']}: {res.error}")
                continue
            fresh = self._metrics_from_company(res.data)
            for key in missing:
                if key in fresh and to_number(fresh[key]) is not None:
                    cand["metrics"][key] = fresh[key]
            if res.data.get("name") and not cand.get("name"):
                cand["name"] = res.data["name"]
        return warnings

    # ------------------------------------------------------------------ #
    # Main
    # ------------------------------------------------------------------ #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        started = time.time()
        as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
        warnings: List[str] = []

        # 1. A provider (or an inline context dict) wins: that is the offline
        #    path and the one the pipeline uses. Only when neither is present do
        #    we reach for the network.
        candidates: Optional[List[Dict[str, Any]]] = None
        supplied = self._collect(context)
        if supplied:
            candidates = list(supplied.get("candidates") or [])
            warnings.extend(supplied.get("warnings") or [])

        # 2. Network path.
        if candidates is None:
            if not bool(self.config.get("enabled", True)):
                return EngineResult(
                    engine=self.name, ok=True,
                    data=self._empty("screener disabled in config"),
                    warnings=["screener disabled"])
            client = self._get_client()
            if self.source == "symbols":
                candidates, w = self._from_symbols(client)
            elif self.source == "screen":
                candidates, w = self._from_screen(client)
            else:
                candidates, w = self._from_sectors(client)
            warnings.extend(w)

            if self.enrich and candidates:
                warnings.extend(self._enrich(client, candidates))

        if not candidates:
            msg = "no candidates obtained"
            if warnings:
                msg += f" ({warnings[0]})"
            self.log(msg)
            return EngineResult(engine=self.name, ok=True,
                                data=self._empty(msg), warnings=warnings)

        # 3. Cap. The cap is applied AFTER acquisition because sector tables
        #    arrive newest-first by market cap, so truncation keeps the biggest
        #    names - the intended behaviour for a liquidity-conscious screen.
        if len(candidates) > self.max_symbols:
            warnings.append(
                f"{len(candidates)} candidates truncated to {self.max_symbols}")
            candidates = candidates[: self.max_symbols]

        # 4. Apply the screen.
        results = self.screen.evaluate(candidates)
        legal_warnings = supplied.get("legal_warnings") or []
        verified_blacklist: Dict[str, Dict[str, str]] = {}
        for item in legal_warnings:
            if not isinstance(item, dict):
                warnings.append("ignored malformed legal warning; expected an object")
                continue
            ticker = str(item.get("ticker") or "").strip().upper()
            reason = " ".join(str(item.get("reason") or "").split()[:10])
            source = str(item.get("source") or "").strip()
            if not ticker or not reason or not source or item.get("verified") is not True:
                warnings.append(
                    "legal warning not applied: ticker, reason, source, and verified=true are required")
                continue
            verified_blacklist[ticker] = {"reason": reason, "source": source}

        for result in results:
            warning = verified_blacklist.get(result.symbol.upper())
            if warning:
                result.selected = False
                result.excluded_by = (
                    f"verified legal/governance warning: {warning['reason']}")
        selected = [r for r in results if r.selected]
        rejected = [r for r in results if not r.selected]

        # 5. Shape the output for the Alpha stage: same keys it already reads.
        fundamentals = self._to_alpha_fundamentals(selected)

        elapsed = round(time.time() - started, 2)
        self.log(
            f"{len(results)} candidates -> {len(selected)} selected "
            f"({len(rejected)} rejected) in {elapsed}s "
            f"[{self.screen.name}]")

        return EngineResult(
            engine=self.name, ok=True,
            data={
                "as_of": as_of,
                "source": self._source_description(),
                "data_kind": "fundamental_snapshot",
                "data_date_note": (
                    "Fundamentals as last reported to screener.in. NOT live "
                    "prices; use OpenAlgo quotes() for live valuation."),
                "screen": self.screen.as_dict(),
                "polite_limits": self.polite_limits(),
                "metrics_derived": self._metrics_derived(),
                "counts": {
                    "candidates": len(results),
                    "selected": len(selected),
                    "rejected": len(rejected),
                    "missing_data": sum(1 for r in results if r.unknown_rules),
                },
                "selected": [r.symbol for r in selected],
                "symbols": [r.symbol for r in selected],
                "asset_blacklist": sorted(verified_blacklist),
                "blacklist_reasons": [
                    {"ticker": ticker, "reason": item["reason"],
                     "source": item["source"]}
                    for ticker, item in sorted(verified_blacklist.items())
                ],
                "fundamentals": fundamentals,
                "results": [r.as_dict() for r in results],
                "rejected": [
                    {"symbol": r.symbol, "reason": r.reason()}
                    for r in rejected
                ],
                "elapsed_seconds": elapsed,
            },
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _collect(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Offline input: context dict first, then provider, then config.

        Matches the convention in ``MarketEngine._collect`` so the pipeline's
        provider injection works identically here.
        """
        if isinstance(context.get("screener_inputs"), dict):
            return context["screener_inputs"]
        if callable(self.provider):
            try:
                data = self.provider()
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                self.log(f"provider failed ({exc!r})")
        if isinstance(self.config.get("screener_inputs"), dict):
            return self.config["screener_inputs"]
        return {}

    def _source_description(self) -> str:
        if self.source == "symbols":
            return f"symbols:{len(self.symbols)}"
        if self.source == "screen":
            return f"screen:{self.screen_id}/{self.screen_slug}"
        return f"sectors:{','.join(self.sectors) or 'none'}"

    def _to_alpha_fundamentals(self, selected: List[Any]) -> Dict[str, Any]:
        """Map screener metrics into the Alpha engine's fundamental keys.

        Alpha reads ``pe``, ``pb``, ``roe``, ``de``, ``dividend_yield``,
        ``earnings_growth`` (see ``alpha/alpha_engine.py`` DEFAULT_FUNDAMENTAL_RULES).
        We supply the subset we can measure and never invent the rest - Alpha
        already handles a partial metric dict by weighting what is present.

        ``roe`` on screener.in is a PERCENT string ("16.5%") while Alpha's rule
        expects a FRACTION (scale 0.30). We divide by 100 here, once, at the
        boundary. Getting this wrong is a silent 100x error in the score, which
        is exactly the kind of bug that never shows up in a backtest because it
        just reshuffles the ranking.
        """
        out: Dict[str, Any] = {}
        for result in selected:
            m = result.metrics or {}
            row: Dict[str, Any] = {}

            pe = to_number(m.get("pe"))
            if pe is not None:
                row["pe"] = pe

            de = to_number(m.get("debt_equity"))
            if de is not None:
                row["de"] = de          # Alpha's key is `de`, not `debt_equity`

            roe_pct = to_number(m.get("roe"))
            if roe_pct is not None:
                row["roe"] = roe_pct / 100.0

            dy_pct = to_number(m.get("dividend_yield"))
            if dy_pct is not None:
                row["dividend_yield"] = dy_pct / 100.0

            # Screener-only extras, kept under their own names so nothing
            # downstream can confuse them with a metric Alpha defines.
            mc = to_number(m.get("market_cap"))
            if mc is not None:
                row["market_cap"] = mc      # in crores, as reported
            for extra in ("roce", "current_price", "sales", "net_profit"):
                val = to_number(m.get(extra))
                if val is not None:
                    row[extra] = val

            row["screener_selected"] = True
            out[result.symbol] = row
        return out

    def _empty(self, note: str) -> Dict[str, Any]:
        return {
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": self._source_description(),
            "data_kind": "fundamental_snapshot",
            "screen": self.screen.as_dict(),
            "polite_limits": self.polite_limits(),
            "metrics_derived": self._metrics_derived(),
            "counts": {"candidates": 0, "selected": 0,
                       "rejected": 0, "missing_data": 0},
            "selected": [], "symbols": [], "fundamentals": {},
            "results": [], "rejected": [],
            "note": note,
        }

    # ------------------------------------------------------------------ #
    # Convenience
    # ------------------------------------------------------------------ #
    def screen_now(self, *, symbols: Optional[List[str]] = None,
                   sector: Optional[str] = None) -> Dict[str, Any]:
        """Run the screen once and return the payload (no EngineResult wrapper).

        The dashboard and the CLI use this; it is the same code path as ``run``.
        """
        if symbols:
            self.source = "symbols"
            self.symbols = [s.upper() for s in symbols]
        if sector:
            self.source = "sector"
            self.sectors = [sector]
        result = self.run({})
        if not result.ok:
            return {"ok": False, "error": result.error, "warnings": result.warnings}
        payload = dict(result.data)
        payload["ok"] = True
        payload["warnings"] = list(result.warnings)
        return payload


# --------------------------------------------------------------------------- #
# Row helpers
# --------------------------------------------------------------------------- #
def _first_present(row: Dict[str, Any], keys) -> Optional[str]:
    """Return the first non-empty value among the accepted column names.

    Match is case-insensitive and tolerant of surrounding whitespace, because
    the exact header text on screener.in tables varies between sector and screen
    views (e.g. "Mar Cap" vs "Market Cap"). Returns ``None`` (not "") when no
    column matches, so the caller can tell "absent" from "blank cell".
    """
    if not isinstance(row, dict):
        return None
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        val = lower.get(key.strip().lower())
        if val is None:
            continue
        text = str(val).strip()
        if text and text not in ("-", "--", "N/A", "n/a"):
            return text
    return None


__all__ = ["ScreenerEngine", "DEFAULT_CACHE_DIR"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# `python -m blue_engine.screener.screener_engine`
#
# A small operator front door. It is deliberately NOT the default way the
# dashboard runs a screen: a sweep fires hundreds of requests at a site we do
# not own, so it is an explicit, terminal-initiated act.
# ---------------------------------------------------------------------------
def _build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m blue_engine.screener.screener_engine",
        description="Run the fundamental screen once and print the result.")
    p.add_argument("--config", default=None,
                   help="path to screener_engine.json (default: input/)")
    p.add_argument("--source", choices=("sector", "symbols", "screen"),
                   default=None, help="override the configured source")
    p.add_argument("--symbols", default=None,
                   help="comma-separated symbols (implies --source symbols)")
    p.add_argument("--sectors", default=None,
                   help="comma-separated sector slugs (implies --source sector)")
    p.add_argument("--max-symbols", type=int, default=None,
                   help="cap the sweep (polite limit)")
    p.add_argument("--save", action="store_true",
                   help="write screener/last_screen.json for the dashboard")
    p.add_argument("--json", action="store_true",
                   help="print the raw JSON payload instead of a table")
    p.add_argument("--enrich", action="store_true",
                   help="fetch each candidate's company page to fill gaps")
    return p


def main(argv=None) -> int:
    """CLI entry point. Returns a process exit code."""
    import json as _json

    args = _build_arg_parser().parse_args(argv)

    cfg_path = Path(args.config) if args.config else \
        (Path(__file__).resolve().parents[1] / "input" / "screener_engine.json")
    engine_cfg: Dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            engine_cfg = (_json.loads(cfg_path.read_text(encoding="utf-8"))
                          .get("screener") or {})
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] could not read {cfg_path}: {exc}")
    else:
        print(f"[warn] config not found at {cfg_path}; using code defaults")

    if args.source:
        engine_cfg["source"] = args.source
    if args.symbols:
        engine_cfg["source"] = "symbols"
        engine_cfg["symbols"] = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.sectors:
        engine_cfg["source"] = "sector"
        engine_cfg["sectors"] = [s.strip() for s in args.sectors.split(",") if s.strip()]
    if args.max_symbols:
        engine_cfg["max_symbols"] = args.max_symbols
    if args.enrich:
        engine_cfg["enrich"] = True

    engine = ScreenerEngine(config=engine_cfg)
    payload = engine.screen_now()

    if args.json:
        print(_json.dumps(payload, indent=2, default=str))
    else:
        _print_report(payload)

    if args.save:
        out = Path(__file__).resolve().parent / "last_screen.json"
        out.write_text(_json.dumps(payload, indent=2, default=str),
                       encoding="utf-8")
        print(f"\n[saved] {out}")

    return 0 if payload.get("ok", False) else 1


def _print_report(payload: Dict[str, Any]) -> None:
    """Human-readable report. ASCII-only: Windows consoles are cp1252 and the
    scraped values carry a rupee sign."""
    def a(text: Any) -> str:
        return str(text).encode("ascii", "backslashreplace").decode("ascii")

    counts = payload.get("counts") or {}
    print("=" * 78)
    print("SCREENER ENGINE")
    print("=" * 78)
    print(f"source      : {payload.get('source')}")
    print(f"as of       : {payload.get('as_of')}")
    print(f"data kind   : {payload.get('data_kind')}")
    print(f"note        : {a(payload.get('data_date_note', ''))}")
    for rule in ((payload.get("screen") or {}).get("rules") or []):
        print(f"rule        : {rule.get('label') or rule.get('metric')}")
    print("-" * 78)
    print(f"candidates  : {counts.get('candidates')}")
    print(f"selected    : {counts.get('selected')}")
    print(f"rejected    : {counts.get('rejected')}")
    print(f"missing data: {counts.get('missing_data')}")
    print("-" * 78)

    rows = payload.get("results") or []
    selected = [r for r in rows if r.get("selected")]
    if selected:
        print("SELECTED:")
        for r in sorted(selected, key=lambda x: x.get("symbol", "")):
            m = r.get("metrics") or {}
            print(f"  {r.get('symbol', ''):<14} "
                  f"mcap={a(m.get('market_cap')):<18} "
                  f"D/E={a(m.get('debt_equity')):<10} "
                  f"PE={a(m.get('pe'))}")
    else:
        print("SELECTED: (none)")

    rejected = [r for r in rows if not r.get("selected")]
    if rejected:
        print("\nREJECTED (reason):")
        for r in sorted(rejected, key=lambda x: x.get("symbol", ""))[:40]:
            print(f"  {r.get('symbol', ''):<14} {a(r.get('reason', ''))[:100]}")
        if len(rejected) > 40:
            print(f"  ... and {len(rejected) - 40} more")

    for w in (payload.get("warnings") or []):
        print(f"[warn] {a(w)}")


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
