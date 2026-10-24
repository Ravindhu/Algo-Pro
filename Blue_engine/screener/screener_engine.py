"""Fetch a saved Screener.in universe for downstream Blue_engine stages.

For ``source="screen"``, Screener.in owns stock selection. This engine fetches
the paginated result, scrapes each selected company's statements at a polite
rate, and publishes raw snapshots to ValuationEngine and ratios to Alpha. It
does not recalculate market cap or debt/equity and does not place orders.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from ..core.base import BaseEngine, EngineResult, EngineStage
from .client.numbers import (
    latest_period_value,
    ratio_lookup,
    table_row_lookup,
    to_number,
)
from .client.screener_client import ScreenerClient
from .filters import ScreenResult, Screener

logger = logging.getLogger(__name__)

#: Default cache/state location, sibling to the engine. Sits next to the engine
#: so a fresh clone has a working cache dir without any setup.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache"


class ScreenerEngine(BaseEngine):
    """Saved-screen scraper backed by the vendored screener-india port.

    Config block (``screener`` in ``screener_engine.json``)::

        {
          "enabled": true,
          "source": "screen",              # primary saved-screen source
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

        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parents[2] / ".env",
                        override=False)
        except ImportError:
            pass

        # Screen definition lives in its own sub-block so a screen can be
        # swapped without touching transport settings.
        self.screen = Screener.from_config(self.config.get("screen") or {})

        self.source = str(self.config.get("source") or "screen").lower()
        self.sectors = list(self.config.get("sectors") or [])
        # SECTOR-UNIVERSE BRIDGE. When ``sector_filter`` is enabled, the sector
        # list is chosen by the measured HM gate (RSI > EMA) instead of being
        # typed into config. An explicit ``sectors`` list still wins, so no
        # existing configuration changes behaviour by accident.
        self.sector_filter = self.config.get("sector_filter") or {}
        self.sector_filter_enabled = bool(
            self.sector_filter.get("enabled", False))
        self.sector_filter_report: Dict[str, Any] = {}
        self.symbols = [str(s).upper() for s in (self.config.get("symbols") or [])]
        self.screen_id = self.config.get("screen_id")
        self.screen_slug = self.config.get("screen_slug")
        screen_url = self.config.get("screen_url") or os.environ.get(
            "URL_SCREENER_DOT_IN")
        if screen_url:
            match = re.search(
                r"/screens/(\d+)/([^/?#]+)", urlparse(str(screen_url)).path)
            if match:
                self.screen_id = self.screen_id or match.group(1)
                self.screen_slug = self.screen_slug or match.group(2)

        self.max_symbols = int(self.config.get("max_symbols", 250))
        self.max_pages = int(self.config.get("max_pages", 40))
        self.include_all_pages = bool(self.config.get("include_all_pages", True))
        #: Enrich screen rows with statements for the downstream valuation stage.
        self.enrich = bool(self.config.get("enrich", self.source == "screen"))
        self.enrich_limit = int(self.config.get("enrich_limit", self.max_symbols))
        self.annual_report_enrichment = bool(
            self.config.get("annual_report_enrichment", self.source == "screen"))

    # ------------------------------------------------------------------ #
    # Client
    # ------------------------------------------------------------------ #
    def _get_client(self) -> ScreenerClient:
        """Build (once) the transport client from config."""
        if self._client_override is not None:
            return self._client_override
        if self._client is None:
            try:
                from dotenv import load_dotenv
                load_dotenv(Path(__file__).resolve().parents[2] / ".env",
                            override=False)
            except ImportError as exc:
                raise RuntimeError(
                    "python-dotenv is required to read the repository .env; "
                    "install the screener extra") from exc
            session_id = (
                os.environ.get("Screenerdotin_sessionid")
                or os.environ.get("SCREENER_SESSION_ID")
                or os.environ.get("SCREENER_SESSIONID")
                or "")
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
                cookies=(str(self.config.get("cookies") or "")
                         or (f"sessionid={session_id}" if session_id else "")),
                cache_dir=cache_dir,
                verify_tls=bool(self.config.get("verify_tls", True)),
            )
            if not session_id and not self.config.get("cookies"):
                username = (
                    os.environ.get("SCREENER_USERNAME")
                    or os.environ.get("Screenerdotin_username")
                    or os.environ.get("username")
                    or os.environ.get("email")
                )
                password = (os.environ.get("SCREENER_PASSWORD")
                            or os.environ.get("password"))
                if username and password:
                    login = self._client.login(username, password)
                    if not login.ok:
                        raise RuntimeError(
                            f"Screener.in authentication failed: {login.error}")
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
    def _apply_sector_filter(self) -> Optional[List[str]]:
        # Replace self.sectors with the sectors the HM gate selects.
        #
        # Returns the warnings to surface, or None when the gate selected
        # nothing - the caller then returns an empty universe instead of
        # screening. Running with an empty sector list would otherwise walk
        # every sector or none, and neither is what the operator asked for.
        #
        # An explicit `sectors` list in config always wins: this only fills the
        # gap when `sector_filter.enabled` is true.
        if not self.sector_filter_enabled:
            return []
        if self.sectors:
            self.sector_filter_report = {
                "applied": False,
                "reason": "explicit sectors list in config takes precedence"}
            return ["sector_filter ignored: config already sets sectors"]
        try:
            from ..sectors.sector_universe_filter import SectorUniverseFilter
            gate = SectorUniverseFilter(self.sector_filter).screener_slugs()
        except Exception as exc:  # noqa: BLE001 - degrade, never halt
            self.sector_filter_report = {
                "applied": False,
                "error": f"{type(exc).__name__}: {exc}"}
            return [f"sector_filter failed ({type(exc).__name__}); "
                    f"universe is empty rather than unfiltered"]

        self.sector_filter_report = dict(gate, applied=bool(gate.get("slugs")))
        self.sectors = list(gate.get("slugs") or [])
        warnings = list(gate.get("warnings") or [])
        if not self.sectors:
            return None
        self.log(
            f"sector filter ({gate.get('rule')}) selected "
            f"{len(gate.get('sectors') or [])} sector(s) -> "
            f"{len(self.sectors)} screener slug(s)")
        return warnings
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
        if not res.ok and "LOGIN_REQUIRED" in (res.error or ""):
            username = (
                os.environ.get("SCREENER_USERNAME")
                or os.environ.get("Screenerdotin_username")
                or os.environ.get("username")
                or os.environ.get("email")
            )
            password = (os.environ.get("SCREENER_PASSWORD")
                        or os.environ.get("password"))
            if username and password:
                login = client.login(username, password)
                if login.ok:
                    res = client.get_screen_details(
                        int(self.screen_id), str(self.screen_slug),
                        include_all_pages=self.include_all_pages,
                        max_pages=self.max_pages)
                else:
                    return [], [f"Screener.in login failed: {login.error}"]
        warnings.extend(res.warnings)
        if not res.ok or not res.data:
            return [], [f"screen {self.screen_id} failed: {res.error}"]
        if res.warnings:
            return [], warnings
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
            "company_data": res.data,
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

        symbol = _first_present(row, ("_symbol", "Symbol", "Ticker"))
        if not symbol:
            label = _first_present(row, ("Name", "Company"))
            if label and re.fullmatch(r"[A-Za-z0-9&._-]{1,20}", label):
                symbol = label
        if not symbol:
            return None
        symbol = str(symbol).strip().upper()
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
        # Preserve blank cells as None; a missing published value is not zero.
        name = _first_present(row, ("Name", "Company")) or symbol
        return {"symbol": symbol, "name": name, "sector": sector, "metrics": raw}

    def _metrics_from_company(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract ratios as published by Screener.in; do not recalculate them."""
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

        metrics["debt_equity"] = de
        metrics["debt_equity_source"] = (
            "reported" if de is not None else "unavailable")
        return metrics

    def _metrics_derived(self) -> Dict[str, str]:
        """This engine reports Screener.in ratios without calculating them."""
        return {}

    # ------------------------------------------------------------------ #
    # Enrichment (opt-in per-symbol fetch)
    # ------------------------------------------------------------------ #
    def _enrich(self, client: ScreenerClient,
                candidates: List[Dict[str, Any]]) -> List[str]:
        """Fill missing metrics from each candidate's own company page.

        Full company statements are retained for valuation. Existing metrics
        from the saved-screen row remain authoritative.
        """
        warnings: List[str] = []
        eligible = candidates[: min(self.enrich_limit, self.max_symbols)]
        for index, cand in enumerate(eligible):
            if index:
                time.sleep(1.5)
            missing = [k for k, v in (cand.get("metrics") or {}).items()
                       if to_number(v) is None]
            res = client.get_company(cand["symbol"], "consolidated")
            if not res.ok or not res.data:
                warnings.append(f"enrich {cand['symbol']}: {res.error}")
                continue
            cand["company_data"] = res.data
            fresh = self._metrics_from_company(res.data)
            for key in missing:
                if key in fresh and to_number(fresh[key]) is not None:
                    cand["metrics"][key] = fresh[key]
            if res.data.get("name") and not cand.get("name"):
                cand["name"] = res.data["name"]
            if self.annual_report_enrichment:
                from .annual_reports import enrich_company_from_report
                fetch_pdf = getattr(client, "fetch_pdf", None)
                if callable(fetch_pdf):
                    report_result = enrich_company_from_report(
                        cand["company_data"], fetch_pdf)
                    if report_result.get("error"):
                        warnings.append(
                            f"annual report {cand['symbol']}: "
                            f"{report_result['error']}")
                else:
                    cand["company_data"]["annual_report_enrichment"] = {
                        "status": "unavailable",
                        "missing_before": [],
                        "filled": [],
                        "missing_after": [],
                        "report": None,
                        "error": "Screener client does not support PDF downloads.",
                    }
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
        from_saved_screen = False
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
            # Resolve the sector universe BEFORE the client is touched, so a
            # gate that selects nothing costs no network calls at all.
            gate_warnings = self._apply_sector_filter()
            if gate_warnings is None:
                return EngineResult(
                    engine=self.name, ok=True,
                    data=self._empty("sector filter selected no sectors"),
                    warnings=list(self.sector_filter_report.get("warnings")
                                  or []))
            warnings.extend(gate_warnings)
            client = self._get_client()
            if self.source == "symbols":
                candidates, w = self._from_symbols(client)
            elif self.source == "screen":
                candidates, w = self._from_screen(client)
                from_saved_screen = True
            else:
                candidates, w = self._from_sectors(client)
            warnings.extend(w)

            if self.enrich and candidates:
                warnings.extend(self._enrich(client, candidates))
                if len(candidates) > min(self.enrich_limit, self.max_symbols):
                    warnings.append(
                        "company-statement enrichment was capped; valuation "
                        "will receive only the enriched subset")

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

        # The configured saved screen is already filtered by Screener.in.
        # Other sources can still use the optional local rule utility.
        if from_saved_screen:
            results = [
                ScreenResult(
                    symbol=str(candidate.get("symbol") or "").upper(),
                    selected=bool(candidate.get("symbol")),
                    metrics=candidate.get("metrics") or {},
                    name=str(candidate.get("name") or ""),
                )
                for candidate in candidates
            ]
        else:
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
        selected_symbols = {result.symbol for result in selected}
        company_snapshots = {
            str(candidate["symbol"]).upper(): candidate["company_data"]
            for candidate in candidates
            if (str(candidate.get("symbol") or "").upper() in selected_symbols
                and isinstance(candidate.get("company_data"), dict))
        }
        annual_report_gaps = {
            symbol: list(
                (snapshot.get("annual_report_enrichment") or {}).get(
                    "missing_after") or [])
            for symbol, snapshot in company_snapshots.items()
            if isinstance(snapshot.get("annual_report_enrichment"), dict)
        }

        elapsed = round(time.time() - started, 2)
        self.log(
            f"{len(results)} candidates -> {len(selected)} selected "
            f"({len(rejected)} rejected) in {elapsed}s "
            f"[{'Screener.in saved screen' if from_saved_screen else self.screen.name}]")

        return EngineResult(
            engine=self.name, ok=True,
            data={
                "as_of": as_of,
                "source": self._source_description(),
                "data_kind": "fundamental_snapshot",
                "data_date_note": (
                    "Fundamentals as last reported to screener.in. NOT live "
                    "prices; use OpenAlgo quotes() for live valuation."),
                "screen": (
                    {
                        "name": "Screener.in saved screen",
                        "id": self.screen_id,
                        "slug": self.screen_slug,
                        "rules_applied_upstream": True,
                    }
                    if from_saved_screen else self.screen.as_dict()
                ),
                "polite_limits": self.polite_limits(),
                "metrics_derived": self._metrics_derived(),
                # Provenance of the universe: which sectors the HM gate chose,
                # and which passed the gate but had no screener equivalent.
                "sector_filter": self.sector_filter_report or None,
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
                "company_snapshots": company_snapshots,
                "annual_report_gaps": annual_report_gaps,
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
                # A provider that was INJECTED and failed must not silently
                # escalate to the live network. Callers inject a provider
                # precisely to keep the engine offline (tests, backtests, and
                # air-gapped runs); falling through would make a unit test hit
                # screener.in and can turn a 3-second test into a 2-minute one.
                # Degrade to "no candidates" instead - the same outcome as a
                # provider that returned no data.
                return {"candidates": [],
                        "warnings": [f"provider failed ({exc!r})"]}
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
            "screen": (
                {
                    "name": "Screener.in saved screen",
                    "id": self.screen_id,
                    "slug": self.screen_slug,
                    "rules_applied_upstream": True,
                }
                if self.source == "screen" else self.screen.as_dict()
            ),
            "polite_limits": self.polite_limits(),
            "metrics_derived": self._metrics_derived(),
            "counts": {"candidates": 0, "selected": 0,
                       "rejected": 0, "missing_data": 0},
            "selected": [], "symbols": [], "fundamentals": {},
            "company_snapshots": {},
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
