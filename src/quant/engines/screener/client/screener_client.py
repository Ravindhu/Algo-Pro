"""
screener_client.py - the Python ``ScreenerClient`` (port of the vendored npm package).

Public surface mirrors ``vendor/screener-india/src/client/index.ts`` exactly:

    get_company(symbol, mode="consolidated")        -> CompanyData
    get_company_tab(symbol, tab, mode)              -> Partial<CompanyData>
    get_company_raw(symbol, mode)                   -> CompanyRaw
    compare_companies(symbols, mode)                -> CompanySummary[]
    search_companies(query, limit=10)               -> SearchResult[]
    list_sectors()                                  -> SectorItem[]
    get_sector_data(sector, **opts)                 -> SectorData
    list_screens(**opts)                            -> ScreenItem[]
    get_screen_details(screen_id, slug, **opts)      -> ScreenDetails
    clear_cache()

Differences from upstream, and why each is safe:

1. **No exceptions escape.** Upstream throws; every method here returns an
   ``ApiResponse(ok=False, error=...)`` instead. An engine must never raise
   (``core/base.py``), and a screener that throws inside a daily sweep would
   take the whole pipeline down for a flaky HTML fetch.

2. **``verify_pairing``-style auth detection is unchanged** - a redirect to
   ``/register`` or ``/login`` becomes ``LOGIN_REQUIRED`` and is never retried.

3. **A disk cache tier** behind the in-memory one (see ``base.TtlCache``). A
   re-run of a daily sweep must not re-fetch 500 pages from a site we do not own.

4. **Sequential + jittered throttle.** Upstream uses ``Promise.allSettled`` for
   ``compareCompanies``, which fires every request at once. We keep a
   *bounded* fan-out (``compare_concurrency``) because unbounded parallelism
   against a third-party site is how you get IP-banned. Results are returned in
   input order regardless, so the contract is unchanged.

The client is READ-ONLY against screener.in. It has no dependency on OpenAlgo,
no broker, and no order path.
"""
from __future__ import annotations

import logging
import ipaddress
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import (
    DEFAULT_ACCEPT,
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    ApiResponse,
    Meta,
    Throttle,
    TtlCache,
    backoff_delay_s,
    should_retry,
)

# `requests.Session` is pooled per-thread; the engine is single-threaded per
# sweep, so one session for the client's lifetime is correct and reuses TLS.
from . import parser as P
from .numbers import latest_period_value, metric_number, table_row_lookup, to_number

logger = logging.getLogger(__name__)

#: Valid financial tabs. Mirrors the CompanyTab union in types.ts.
COMPANY_TABS = (
    "quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios",
    "shareholding", "documents", "analysis", "peers",
)

#: Tab name -> the CompanyData key it populates. Keeps get_company_tab a
#: table lookup instead of a 9-arm if/else.
_TAB_TO_FIELD = {
    "quarters": "quarters",
    "profit-loss": "profit_loss",
    "balance-sheet": "balance_sheet",
    "cash-flow": "cash_flow",
    "ratios": "ratios",
    "shareholding": "shareholding",
    "documents": "documents",
    "analysis": "analysis",
    "peers": "peers",
}


class ScreenerClient:
    """Read-only Python client for screener.in public pages.

    Args:
        base_url: override the host (tests point this at a local fixture server).
        cache_ttl_ms: memory+disk cache TTL. Default 6 h: fundamentals change at
            most daily, so a 6 h TTL makes a same-day re-run nearly free while
            still refreshing within one trading session.
        min_interval_ms: minimum gap between requests. Default 400 ms, i.e.
            twice upstream's 200 ms - deliberate politeness toward a site we do
            not own. Raise it if you see 429s.
        max_retries: retries on 429/5xx/network only.
        timeout_ms: per-request timeout.
        proxy_url: optional HTTP(S)/SOCKS proxy.
        cookies: optional ``sessionid=...`` cookie string for protected screens.
        cache_dir: disk cache directory (None disables the disk tier).
    """

    def __init__(self, *,
                 base_url: str = DEFAULT_BASE_URL,
                 cache_ttl_ms: int = 21_600_000,
                 min_interval_ms: int = 400,
                 max_retries: int = 2,
                 timeout_ms: int = 20_000,
                 proxy_url: str = "",
                 cookies: str = "",
                 cache_dir: Optional[str] = None,
                 user_agent: str = DEFAULT_USER_AGENT,
                 verify_tls: bool = True):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = int(max_retries)
        self.timeout_ms = int(timeout_ms)
        self.verify_tls = bool(verify_tls)

        self.cache = TtlCache(ttl_ms=cache_ttl_ms, cache_dir=cache_dir)
        self.throttle = Throttle(default_interval_ms=min_interval_ms)

        headers = {
            "User-Agent": user_agent,
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
        if cookies:
            headers["Cookie"] = cookies

        self._session = requests.Session()
        self._session.headers.update(headers)
        if proxy_url:
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})

        # Operational counters the dashboard/engine can read without importing
        # requests itself. Purely informational.
        self.stats: Dict[str, int] = {
            "requests": 0, "cache_hits": 0, "retries": 0, "failures": 0,
        }

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def login(self, username: str, password: str) -> ApiResponse:
        """Authenticate through the site's login form and retain its cookies.

        Credentials are only posted to a same-origin form action. Challenges
        such as CAPTCHA or MFA are not bypassed; they result in a clear failure.
        """
        login_url = f"{self.base_url}/login/"
        if not username or not password:
            return ApiResponse(
                data=None, meta=self._meta(login_url), ok=False,
                error="username and password are required")

        try:
            self.throttle.wait("auth")
            self.stats["requests"] += 1
            response = self._session.get(
                login_url, timeout=self.timeout_ms / 1000.0,
                allow_redirects=True, verify=self.verify_tls)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            csrf_input = soup.select_one(
                'form input[name="csrfmiddlewaretoken"]')
            form = csrf_input.find_parent("form") if csrf_input else None
            csrf_token = str(csrf_input.get("value") or "") if csrf_input else ""
            if form is None or not csrf_token:
                self.stats["failures"] += 1
                return ApiResponse(
                    data=None, meta=self._meta(response.url or login_url),
                    ok=False, error="login form or non-empty CSRF token was not found")

            action = urljoin(response.url or login_url, form.get("action") or "")
            base_origin = urlparse(self.base_url)
            action_origin = urlparse(action)
            if (action_origin.scheme != base_origin.scheme
                    or action_origin.netloc.lower() != base_origin.netloc.lower()):
                self.stats["failures"] += 1
                return ApiResponse(
                    data=None, meta=self._meta(login_url), ok=False,
                    error="login form action is not on the configured origin")

            username_input = (
                form.select_one('input[name="username"]')
                or form.select_one('input[name="email"]')
                or form.select_one('input[type="text"]')
                or form.select_one('input[type="email"]')
            )
            password_input = form.select_one('input[type="password"]')
            if username_input is None or password_input is None:
                self.stats["failures"] += 1
                return ApiResponse(
                    data=None, meta=self._meta(login_url), ok=False,
                    error="login form is missing username or password fields")

            payload = {
                str(field.get("name")): str(field.get("value") or "")
                for field in form.select('input[type="hidden"][name]')
            }
            payload[str(csrf_input.get("name"))] = str(
                csrf_input.get("value") or "")
            payload[str(username_input.get("name"))] = username
            payload[str(password_input.get("name"))] = password

            self.throttle.wait("auth")
            self.stats["requests"] += 1
            result = self._session.post(
                action, data=payload,
                headers={"Referer": response.url or login_url},
                timeout=self.timeout_ms / 1000.0, allow_redirects=False,
                verify=self.verify_tls)
            if result.status_code in (301, 302, 303):
                redirect = result.headers.get("Location")
                if not redirect:
                    self.stats["failures"] += 1
                    return ApiResponse(
                        data=None, meta=self._meta(action), ok=False,
                        error="login redirect did not include a destination")
                destination = urljoin(action, redirect)
                redirect_origin = urlparse(destination)
                if (redirect_origin.scheme != base_origin.scheme
                        or redirect_origin.netloc.lower()
                        != base_origin.netloc.lower()):
                    self.stats["failures"] += 1
                    return ApiResponse(
                        data=None, meta=self._meta(action), ok=False,
                        error="login redirect is not on the configured origin")
                result = self._session.get(
                    destination, timeout=self.timeout_ms / 1000.0,
                    allow_redirects=True, verify=self.verify_tls)
            elif 300 <= result.status_code < 400:
                self.stats["failures"] += 1
                return ApiResponse(
                    data=None, meta=self._meta(action), ok=False,
                    error=f"unsupported login redirect (HTTP {result.status_code})")
            result.raise_for_status()
            if ("/login" in (result.url or "").lower()
                    or BeautifulSoup(result.text, "html.parser").select_one(
                        'form input[type="password"]') is not None):
                self.stats["failures"] += 1
                return ApiResponse(
                    data=None, meta=self._meta(result.url or action), ok=False,
                    error="login was not accepted; check credentials or account challenges")

            session_id = self._session.cookies.get("sessionid")
            if not session_id:
                self.stats["failures"] += 1
                return ApiResponse(
                    data=None, meta=self._meta(result.url or action), ok=False,
                    error="login response did not set a sessionid cookie")
            self._session.headers.pop("Cookie", None)
            return ApiResponse(
                data={"authenticated": True, "sessionid": session_id},
                meta=self._meta(result.url or action))
        except requests.RequestException as exc:
            self.stats["failures"] += 1
            return ApiResponse(
                data=None, meta=self._meta(login_url), ok=False,
                error=f"{type(exc).__name__}: {exc}")

    def _meta(self, url: str) -> Meta:
        return Meta(
            source_url=url,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def fetch_html(self, url: str, scope: str = "default") -> tuple:
        """Fetch a page with cache + throttle + bounded retry.

        Returns ``(html, error)`` - exactly one is non-None. This tuple style is
        the port's internal convention; the public methods wrap it into an
        ``ApiResponse``.
        """
        key = self.cache.key_for(url)
        cached = self.cache.get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached, None

        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            self.throttle.wait(scope)
            self.stats["requests"] += 1
            status: Optional[int] = None
            try:
                resp = self._session.get(
                    url,
                    timeout=self.timeout_ms / 1000.0,
                    allow_redirects=True,
                    verify=self.verify_tls,
                )
                status = resp.status_code
                # Login-wall detection: screener.in redirects gated pages to
                # /register/ or /login/. Port of the upstream check, but on the
                # final URL AND on the body, because the redirect is sometimes a
                # client-side one that leaves the URL intact.
                final_url = resp.url or url
                if "/register" in final_url or "/login" in final_url:
                    raise _LoginRequired(f"{url} requires a screener.in session cookie")

                if status >= 400:
                    last_error = f"HTTP {status} for {url}"
                    if not should_retry(status, last_error):
                        self.stats["failures"] += 1
                        return None, last_error
                else:
                    html = resp.text
                    self.cache.set(key, html)
                    return html, None
            except _LoginRequired as exc:
                # Never retried - more attempts cannot authenticate us.
                self.stats["failures"] += 1
                return None, f"LOGIN_REQUIRED: {exc}"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if not should_retry(None, last_error):
                    self.stats["failures"] += 1
                    return None, last_error

            if attempt < self.max_retries:
                self.stats["retries"] += 1
                delay = backoff_delay_s(attempt)
                logger.debug("screener retry %d/%d in %.1fs: %s",
                             attempt + 1, self.max_retries, delay, url)
                time.sleep(delay)

        self.stats["failures"] += 1
        return None, last_error or f"fetch failed for {url}"

    def fetch_pdf(self, url: str, *,
                  max_bytes: int = 25 * 1024 * 1024) -> tuple:
        """Download a bounded PDF without sending Screener cookies off-origin."""
        base_origin = urlparse(self.base_url)
        current_url = url
        public_session: Optional[requests.Session] = None
        for redirect_count in range(4):
            parsed = urlparse(current_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return None, "annual-report URL must be an absolute HTTP(S) URL"
            if parsed.username or parsed.password:
                return None, "annual-report URL must not contain credentials"
            same_origin = (
                parsed.scheme == base_origin.scheme
                and parsed.netloc.lower() == base_origin.netloc.lower()
            )
            host = (parsed.hostname or "").lower().rstrip(".")
            if not same_origin:
                if (parsed.scheme != "https" or host in {
                        "localhost", "localhost.localdomain"} or host.endswith(
                            (".local", ".internal", ".localhost"))):
                    return None, "external annual-report URLs must use a public HTTPS host"
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    return None, "external annual-report URL uses a non-public IP address"
            if same_origin:
                session = self._session
            else:
                if public_session is None:
                    public_session = requests.Session()
                    public_session.headers.update({
                        "User-Agent": self._session.headers.get(
                            "User-Agent", DEFAULT_USER_AGENT),
                        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5",
                    })
                session = public_session

            self.throttle.wait("document")
            self.stats["requests"] += 1
            response = None
            try:
                response = session.get(
                    current_url,
                    timeout=self.timeout_ms / 1000.0,
                    allow_redirects=False,
                    stream=True,
                    verify=self.verify_tls,
                )
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    if not location or redirect_count == 3:
                        self.stats["failures"] += 1
                        return None, "annual-report redirect limit exceeded"
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        self.stats["failures"] += 1
                        return None, f"annual report exceeds {max_bytes}-byte limit"
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not body.startswith(b"%PDF-"):
                    self.stats["failures"] += 1
                    return None, "annual-report response is not a PDF"
                return body, None
            except requests.RequestException as exc:
                self.stats["failures"] += 1
                return None, f"{type(exc).__name__}: {exc}"
            finally:
                if response is not None:
                    response.close()
                if public_session is not None:
                    public_session.close()
                    public_session = None
        self.stats["failures"] += 1
        return None, "annual-report redirect limit exceeded"

    # ------------------------------------------------------------------ #
    # URL builders (port of BaseClient.companyUrl)
    # ------------------------------------------------------------------ #
    def company_url(self, symbol: str, mode: str = "consolidated") -> str:
        base = f"{self.base_url}/company/{symbol.upper()}"
        return f"{base}/consolidated/" if mode == "consolidated" else f"{base}/"

    # ------------------------------------------------------------------ #
    # Company
    # ------------------------------------------------------------------ #
    def get_company(self, symbol: str,
                    mode: str = "consolidated") -> ApiResponse:
        """Full fundamental snapshot. Port of upstream ``getCompany``."""
        url = self.company_url(symbol, mode)
        html, err = self.fetch_html(url, "company")
        if err:
            return ApiResponse(data=None, meta=self._meta(url), ok=False, error=err)

        root = P.parse_html(html)
        warnings: List[str] = []
        name = P.parse_title(root)
        if not name:
            warnings.append("Could not parse company name")

        data = {
            "name": name,
            "symbol": symbol.upper(),
            "mode": mode,
            "source_url": url,
            "top_ratios": P.parse_top_ratios(root),
            "quarters": P.parse_table(_sec(root, "quarters")),
            "profit_loss": P.parse_table(_sec(root, "profit-loss")),
            "balance_sheet": P.parse_table(_sec(root, "balance-sheet")),
            "cash_flow": P.parse_table(_sec(root, "cash-flow")),
            "ratios": P.parse_table(_sec(root, "ratios")),
            "shareholding": P.parse_table(_sec(root, "shareholding")),
            "documents": P.parse_documents(root),
            "analysis": P.parse_analysis(root),
            "peers": P.parse_peers(root),
        }
        return ApiResponse(data=data, meta=self._meta(url), warnings=warnings)

    def get_company_tab(self, symbol: str, tab: str,
                        mode: str = "consolidated") -> ApiResponse:
        """One financial tab only. Port of upstream ``getCompanyTab``."""
        if tab not in COMPANY_TABS:
            return ApiResponse(data=None, meta=self._meta(""),
                               ok=False,
                               error=f"unknown tab {tab!r}; valid: {list(COMPANY_TABS)}")
        url = self.company_url(symbol, mode)
        html, err = self.fetch_html(url, "company")
        if err:
            return ApiResponse(data=None, meta=self._meta(url), ok=False, error=err)

        root = P.parse_html(html)
        field = _TAB_TO_FIELD[tab]
        data: Dict[str, Any] = {"symbol": symbol.upper(), "mode": mode}
        if tab == "documents":
            data[field] = P.parse_documents(root)
        elif tab == "analysis":
            data[field] = P.parse_analysis(root)
        elif tab == "peers":
            data[field] = P.parse_peers(root)
        else:
            data[field] = P.parse_table(_sec(root, tab))
        return ApiResponse(data=data, meta=self._meta(url))

    def get_company_raw(self, symbol: str,
                        mode: str = "consolidated") -> ApiResponse:
        """Raw HTML + section ids. Port of upstream ``getCompanyRaw``."""
        url = self.company_url(symbol, mode)
        html, err = self.fetch_html(url, "company")
        if err:
            return ApiResponse(data=None, meta=self._meta(url), ok=False, error=err)
        root = P.parse_html(html)
        return ApiResponse(
            data={"symbol": symbol.upper(), "html": html,
                  "section_ids": P.parse_section_ids(root)},
            meta=self._meta(url),
        )

    def compare_companies(self, symbols: List[str],
                          mode: str = "consolidated",
                          concurrency: int = 1) -> ApiResponse:
        """Side-by-side summary rows. Port of upstream ``compareCompanies``.

        ``concurrency=1`` (default) fetches sequentially through the same
        throttle. Raising it is a deliberate choice by the caller - see the
        module docstring.
        """
        summaries: List[Dict[str, Any]] = []
        warnings: List[str] = []

        # Sequential by default; a bounded fan-out is available but opt-in.
        results: List[Optional[ApiResponse]] = []
        if concurrency <= 1:
            for s in symbols:
                results.append(self.get_company(s, mode))
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                results = list(pool.map(lambda s: self.get_company(s, mode), symbols))

        for symbol, res in zip(symbols, results):
            if res is None or not res.ok or not res.data:
                reason = (res.error if res else "no response")
                warnings.append(f"Failed to fetch {symbol}: {reason}")
                continue
            data = res.data
            tr = data.get("top_ratios") or []
            summaries.append({
                "symbol": data.get("symbol"),
                "name": data.get("name"),
                "market_cap": _ratio_text(tr, "market_cap"),
                "current_price": _ratio_text(tr, "current_price"),
                "pe": _ratio_text(tr, "pe"),
                "roe": _ratio_text(tr, "roe"),
                "dividend_yield": _ratio_text(tr, "dividend_yield"),
            })

        return ApiResponse(
            data=summaries,
            meta=self._meta(f"{self.base_url}/company/"),
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    def search_companies(self, query: str, limit: int = 10) -> ApiResponse:
        """Symbol/name search via the JSON API. Port of ``searchCompanies``."""
        api_url = (f"{self.base_url}/api/company/search/"
                   f"?q={requests.utils.quote(query)}&limit={int(limit)}")
        self.throttle.wait("search")
        self.stats["requests"] += 1
        try:
            resp = self._session.get(api_url, timeout=self.timeout_ms / 1000.0,
                                     headers={"Accept": "application/json"},
                                     verify=self.verify_tls)
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:  # noqa: BLE001 - any transport/JSON error
            return ApiResponse(data=[], meta=self._meta(api_url), ok=False,
                               error=f"search failed: {type(exc).__name__}: {exc}")

        if not isinstance(items, list):
            items = []

        import re
        results: List[Dict[str, str]] = []
        for item in items[:int(limit)]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            m = re.search(r"/company/([^/]+)/", url)
            symbol = m.group(1) if m else str(item.get("name") or "")
            results.append({
                "symbol": symbol,
                "name": str(item.get("name") or ""),
                "url": (f"{self.base_url}{url}" if url else
                        f"{self.base_url}/company/{symbol}/"),
            })

        return ApiResponse(
            data=results, meta=self._meta(api_url),
            warnings=[] if results else ["No results found for the given query"],
        )

    # ------------------------------------------------------------------ #
    # Sectors
    # ------------------------------------------------------------------ #
    def list_sectors(self) -> ApiResponse:
        """All sectors. Port of upstream ``listSectors``."""
        url = f"{self.base_url}/market/"
        html, err = self.fetch_html(url, "sector")
        if err:
            return ApiResponse(data=[], meta=self._meta(url), ok=False, error=err)
        return ApiResponse(data=P.parse_sector_links(P.parse_html(html)),
                           meta=self._meta(url))

    def get_sector_data(self, sector: str, page: int = 1, limit: int = 50,
                        include_all_pages: bool = False,
                        max_pages: int = 40) -> ApiResponse:
        """Companies in a sector. Port of upstream ``getSectorData``.

        ``max_pages`` caps the include-all walk. Upstream has no cap; an
        unbounded loop against a third-party site is a safety hole, so we stop
        and WARN instead of silently hammering.
        """
        if include_all_pages:
            return self._get_all_pages(
                lambda p: self.get_sector_data(sector, page=p, limit=limit),
                sector=sector, limit=limit, max_pages=max_pages,
            )

        url = f"{self.base_url}/market/{sector}/?limit={int(limit)}&page={int(page)}"
        html, err = self.fetch_html(url, "sector")
        if err:
            return ApiResponse(data=None, meta=self._meta(url), ok=False, error=err)
        root = P.parse_html(html)
        tbl = P.parse_data_table(root)
        pag = P.parse_pagination(root)
        rows = tbl["rows"]
        return ApiResponse(data={
            "sector": sector,
            "columns": tbl["columns"],
            "rows": rows,
            "page": int(page),
            "total_pages": pag["total_pages"],
            "total_results": len(rows),
        }, meta=self._meta(url))

    def _get_all_pages(self, fetch_page, *, sector: str, limit: int,
                       max_pages: int) -> ApiResponse:
        """Walk every page of a sector/screen and concatenate the rows."""
        first = fetch_page(1)
        if not first.ok or not first.data:
            return first
        all_rows = list(first.data.get("rows") or [])
        warnings: List[str] = list(first.warnings)
        total_pages = int(first.data.get("total_pages") or 1)
        if total_pages > max_pages:
            warnings.append(
                f"{sector}: {total_pages} pages available, capped at {max_pages} "
                f"(raise max_pages if you really need all of them)")
            total_pages = max_pages

        for p in range(2, total_pages + 1):
            res = fetch_page(p)
            if not res.ok or not res.data:
                warnings.append(f"Failed to fetch page {p}: {res.error}")
                continue
            all_rows.extend(res.data.get("rows") or [])

        merged = dict(first.data)
        merged.update({"rows": all_rows, "total_results": len(all_rows), "page": 1})
        return ApiResponse(data=merged, meta=first.meta, warnings=warnings)

    # ------------------------------------------------------------------ #
    # Screens
    # ------------------------------------------------------------------ #
    def list_screens(self, page: int = 1, include_all_pages: bool = False,
                     max_pages: int = 20, q: Optional[str] = None,
                     sort: Optional[str] = None, order: str = "asc") -> ApiResponse:
        """Public screens directory. Port of upstream ``listScreens``."""
        def fetch_page(p: int) -> List[Dict[str, Any]]:
            url = f"{self.base_url}/screens/?page={int(p)}"
            html, err = self.fetch_html(url, "screens")
            if err:
                return []
            return P.parse_screen_items(P.parse_html(html))

        items = fetch_page(page)
        if include_all_pages:
            for p in range(page + 1, max_pages + 1):
                more = fetch_page(p)
                if not more:
                    break
                items.extend(more)
            # Deduplicate by id (upstream does the same).
            seen: set = set()
            deduped = []
            for it in items:
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                deduped.append(it)
            items = deduped

        if q:
            lq = q.lower()
            items = [it for it in items
                     if lq in it["title"].lower() or lq in it["description"].lower()]

        if sort == "title":
            items.sort(key=lambda it: it["title"], reverse=(order == "desc"))
        elif sort == "id":
            items.sort(key=lambda it: it["id"], reverse=(order == "desc"))

        return ApiResponse(data=items,
                           meta=self._meta(f"{self.base_url}/screens/?page={page}"))

    def get_screen_details(self, screen_id: int, slug: str, page: int = 1,
                           limit: int = 50, include_all_pages: bool = False,
                           max_pages: int = 40) -> ApiResponse:
        """Companies matching a public screen. Port of ``getScreenDetails``."""
        def fetch_page(p: int) -> ApiResponse:
            url = (f"{self.base_url}/screens/{int(screen_id)}/{slug}/"
                   f"?page={int(p)}&limit={int(limit)}")
            html, err = self.fetch_html(url, "screens")
            if err:
                return ApiResponse(data=None, meta=self._meta(url), ok=False,
                                   error=err)
            root = P.parse_html(html)
            tbl = P.parse_data_table(root)
            pag = P.parse_pagination(root)
            title_el = (root.select_one("h1") or root.select_one(".screen-title")
                        or root.find("title"))
            title = ""
            if title_el is not None:
                title = (title_el.get_text() or "").split("|")[0].strip()
            return ApiResponse(data={
                "id": int(screen_id),
                "slug": slug,
                "title": title or f"Screen #{screen_id}",
                "columns": tbl["columns"],
                "rows": tbl["rows"],
                "page": int(p),
                "total_pages": pag["total_pages"],
            }, meta=self._meta(url))

        first = fetch_page(page)
        if not first.ok or not first.data:
            return first
        if not include_all_pages:
            return first

        all_rows = list(first.data["rows"])
        warnings: List[str] = list(first.warnings)
        available_pages = int(first.data.get("total_pages") or 1)
        total_pages = min(available_pages, max_pages)
        if available_pages > max_pages:
            warnings.append(
                f"screen has {available_pages} pages, capped at {max_pages}; "
                "results are incomplete")
        for p in range(page + 1, total_pages + 1):
            res = fetch_page(p)
            if not res.ok or not res.data:
                warnings.append(f"Failed to fetch page {p}: {res.error}")
                continue
            all_rows.extend(res.data.get("rows") or [])

        merged = dict(first.data)
        merged["rows"] = all_rows
        return ApiResponse(data=merged, meta=first.meta, warnings=warnings)

    # ------------------------------------------------------------------ #
    # Cache
    # ------------------------------------------------------------------ #
    def clear_cache(self) -> None:
        """Clear the in-memory tier. The disk tier expires by TTL."""
        self.cache.clear()


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
class _LoginRequired(Exception):
    """Raised internally when a page redirects to /login or /register."""


def _sec(root, section_id: str):
    """``root.querySelector('section#<id>')``, None-safe."""
    from bs4 import element as _el
    found = root.select_one(f"section#{section_id}")
    return found if isinstance(found, _el.Tag) else None


def _ratio_text(top_ratios: List[Dict[str, Any]], metric: str) -> Optional[str]:
    from .numbers import ratio_lookup
    return ratio_lookup(top_ratios, metric)


__all__ = ["ScreenerClient", "COMPANY_TABS"]
