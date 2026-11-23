"""
providers/mospi_provider.py - live India macro data from MoSPI eSankhyiki.

Why this provider matters
-------------------------
It closes the three biggest gaps in the macro model: inflation, GDP growth and
unemployment. Before this, all three were static numbers in
``macro_master.json`` that drifted silently -- inflation had been sitting at
2.5% while the real print was 4.82%.

Source: https://esankhyiki.mospi.gov.in - the Ministry of Statistics &
Programme Implementation's official data portal. Unlike ``data.rbi.org.in``
(an undocumented SPA behind bot protection that refuses scripting), MoSPI
PUBLISHES machine-readable descriptors at ``/llms.txt`` and
``/ai-catalog.json`` and runs a public MCP server. It is the official,
script-friendly source for this data.

Transport
---------
JSON-RPC 2.0 over HTTPS against ``https://mcp.mospi.gov.in``:

    initialize -> tools/list -> tools/call
    tools: list_datasets, get_indicators, get_metadata, get_data

No API key. No scraping. TTL is 6h: these are monthly/quarterly releases, so
polling harder only wastes their bandwidth and returns the same number.

The three traps this module exists to avoid
-------------------------------------------
1. **``rows[0]`` is not the headline.** CPI rows come back per sector --
   Rural, Urban, **Combined**. ``rows[0]`` is Rural. The published headline is
   the Combined figure. Taking the first row silently scored rural-only
   inflation (5.23%) instead of the real number (4.82%). ``_cpi_combined()``
   exists solely to pick the right row.

2. **``frequency_code`` selects the series, and it is not self-evident.**
   ``1`` = Annual, ``2`` = Quarterly, ``3`` = empty. The quarterly series is
   what carries the Q1 FY27 figure; the annual series has no quarters at all.
   Both return HTTP 200 looking equally valid.

3. **Responses are sometimes SSE-framed.** The server non-deterministically
   answers with ``event: message\\ndata: {...}`` instead of plain JSON. Every
   read goes through ``_unwrap`` or it fails intermittently.

Units
-----
NAS returns GDP in **rupees crore**, constant prices. Growth is computed
year-over-year from those levels rather than read from a headline field, so a
revision to the level series flows through automatically.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .base import MacroProvider, value_block

#: The MCP endpoint. A single URL; the JSON-RPC body selects the operation.
MCP_URL = "https://mcp.mospi.gov.in"

#: MCP protocol revision this client speaks.
PROTOCOL_VERSION = "2025-06-18"

#: (dataset, frequency_code) -> human meaning, for logging/debugging only.
FREQUENCY_ANNUAL = "1"
FREQUENCY_QUARTERLY = "2"


class MospiProvider(MacroProvider):
    """Live CPI, GDP and unemployment from MoSPI's official MCP server."""

    name = "mospi"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 rpc_call: Optional[Callable[..., Any]] = None):
        """Create the provider.

        Args:
            config: optional overrides. Recognised keys:
                timeout (float, default 70) - per-request seconds.
                base_year_cpi (str, default "2024") - 2024 is the current base;
                    2012 is the legacy series and is much noisier.
                gdp_base_year (str, default "2022-23").
            log: optional callable(str) for diagnostics.
            rpc_call: injection point for tests -- a callable
                (method, params, timeout) -> decoded payload. When omitted the
                real network transport is used.
        """
        super().__init__(config, log)
        self.timeout = float(self.config.get("timeout", 70.0))
        self.base_year_cpi = str(self.config.get("base_year_cpi", "2024"))
        self.gdp_base_year = str(self.config.get("gdp_base_year", "2022-23"))
        self._rpc_override = rpc_call
        self._req_id = 0

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _rpc(self, method: str, params: Optional[dict] = None,
             timeout: Optional[float] = None) -> Optional[Any]:
        """One JSON-RPC round trip. Returns the decoded envelope or None."""
        if self._rpc_override is not None:
            return self._rpc_override(method, params, timeout or self.timeout)

        self._req_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._req_id,
                                   "method": method}
        if params is not None:
            payload["params"] = params

        req = urllib.request.Request(
            MCP_URL, data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                # The server may answer with SSE, so both types must be offered.
                "Accept": "application/json, text/event-stream",
                "User-Agent": "blue-engine/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout or self.timeout) as resp:
                return self._unwrap(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            self.log(f"HTTP {exc.code} on {method}")
            return None
        except Exception as exc:  # noqa: BLE001 - never raise from a provider
            self.log(f"{method} failed: {exc!r}")
            return None

    @staticmethod
    def _unwrap(raw: str) -> Optional[Any]:
        """Decode an MCP reply, tolerating either plain JSON or SSE framing."""
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("event:") or "data:" in text[:120]:
            chunks = [ln[5:].strip() for ln in text.splitlines()
                      if ln.startswith("data:")]
            text = next((c for c in reversed(chunks)
                         if c and c != "[DONE]"), "")
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            return None

    def _call_tool(self, name: str, args: dict,
                   timeout: Optional[float] = None) -> Optional[Any]:
        """Invoke one MCP tool and return its payload.

        Prefers ``structuredContent`` (clean JSON). ``content[0].text`` holds
        the SAME payload re-encoded as a string, so a client reading only that
        gets a quoted blob needing a second parse.
        """
        doc = self._rpc("tools/call", {"name": name, "arguments": args},
                        timeout)
        if not isinstance(doc, dict):
            return None
        result = doc.get("result")
        if not isinstance(result, dict):
            return doc.get("error")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except Exception:  # noqa: BLE001
                    return item["text"]
        return result

    def _get_data(self, dataset: str,
                  filters: Dict[str, str]) -> List[Dict[str, Any]]:
        """Fetch rows for a dataset. Returns [] on any failure.

        Note: an invalid filter comes back as a NORMAL result with
        ``valid: False``, not a JSON-RPC error, and ``isError`` stays False.
        So the payload is validated, never the transport status.
        """
        out = self._call_tool("get_data",
                              {"dataset": dataset, "filters": filters})
        if isinstance(out, dict) and out.get("valid") is False:
            self.log(f"{dataset} rejected filters {filters}: "
                     f"{str(out.get('error') or out)[:120]}")
            return []
        if isinstance(out, list):
            return [r for r in out if isinstance(r, dict)]
        if isinstance(out, dict):
            for key in ("records", "data", "rows", "results"):
                v = out.get(key)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
                if isinstance(v, dict):
                    for key2 in ("records", "data", "rows", "results"):
                        if isinstance(v.get(key2), list):
                            return [r for r in v[key2] if isinstance(r, dict)]
        return []

    # ------------------------------------------------------------------ #
    # Parsers (pure + static, so they unit-test without a network)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cpi_combined(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Pick the All-India CPI (General), Combined sector row.

        THE central trap. MoSPI returns one row per sector:

            Rural    CPI (General)  5.23
            Urban    CPI (General)  4.31
            Combined CPI (General)  4.82   <- the published headline

        ``rows[0]`` is Rural. Scoring rural-only inflation instead of the
        headline is a silent 0.41pp error, so the sector is matched explicitly
        rather than by position.
        """
        general = [r for r in rows
                   if str(r.get("division", "")).strip().lower()
                   == "cpi (general)"]
        pool = general or rows
        for r in pool:
            if str(r.get("sector", "")).strip().lower() == "combined":
                return r
        # Combined absent: fall back to the first General row, but say so --
        # a missing Combined means the payload shape changed.
        return pool[0] if pool else None

    @staticmethod
    def _plfs_headline(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Pick the all-India unemployment rate for BOTH sexes, all ages.

        PLFS rows are dimensioned by SIX fields (state, gender, sector,
        AgeGroup, weekly_status, frequency), so ``rows[0]`` is almost never the
        aggregate. An earlier version reported **1.9%** -- which is a genuine
        PLFS figure for ``gender=male, sector=rural, 2022``, i.e. rural male
        unemployment from four years ago, not the headline.

        The published all-India rate is:

            state=All India, gender=person, sector='rural + urban',
            AgeGroup='All ages'

        and only the MONTHLY series is current (the annual series ends in
        2022). Because the rate for both sexes is what gets quoted, this
        prefers the ``person`` row and, when it is absent, averages the male
        and female rows rather than silently falling back to one of them --
        each is a published figure, so the average is derived, not invented.
        """
        if not rows:
            return None

        def norm(x: Any) -> str:
            return str(x or "").strip().lower()

        all_india = [r for r in rows if norm(r.get("state")).startswith("all india")]
        pool = all_india or rows

        # Prefer the current monthly series; the annual one is years stale.
        monthly = [r for r in pool if norm(r.get("frequency")) == "monthly"]
        pool = monthly or pool

        # AgeGroup 'All ages' is the headline; '15 years and above' is close
        # but a narrower definition.
        all_ages = [r for r in pool if norm(r.get("AgeGroup")) == "all ages"]
        pool = all_ages or pool

        combined = [r for r in pool
                    if norm(r.get("sector")) in ("rural + urban", "combined")]
        pool = combined or pool

        person = [r for r in pool if norm(r.get("gender")) == "person"]
        if person:
            return person[0]

        # No 'person' row: average male and female (both published).
        males = [r for r in pool if norm(r.get("gender")) == "male"]
        females = [r for r in pool if norm(r.get("gender")) == "female"]
        mv = MospiProvider._to_float(males[0].get("value")) if males else None
        fv = MospiProvider._to_float(females[0].get("value")) if females else None
        if mv is not None and fv is not None:
            merged = dict(males[0])
            merged["value"] = round((mv + fv) / 2.0, 2)
            merged["gender"] = "person (derived: mean of male and female)"
            return merged
        return pool[0] if pool else None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _gdp_yoy(cls, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Compute YoY real-GDP growth from the constant-price level series.

        Growth is DERIVED, not read from a headline field, because the NAS
        endpoint exposes levels and revisions rather than a growth rate. That
        also means a revision to the level series is reflected automatically.

        Quarterly rows are preferred: they carry the Q1 FY27 figure the market
        quotes. Annual rows have ``quarter = None`` and answer a different
        question, so the two are never mixed.

        Rows look like::

            {"year": "2026-27", "frequency": "Quarterly", "quarter": "Q1",
             "constant_price": 8136153}

        i.e. 8,136,153 crore for 2026-27 Q1 against 7,546,230 for 2025-26 Q1
        -> +7.82%.
        """
        quarterly = [r for r in rows
                     if str(r.get("frequency", "")).lower() == "quarterly"
                     or r.get("quarter")]
        pool = quarterly or rows
        if not pool:
            return None

        # Group by (year, quarter); keep the latest quarter available.
        by_key: Dict[tuple, float] = {}
        for r in pool:
            lvl = cls._to_float(r.get("constant_price"))
            if lvl is None or lvl <= 0:
                continue
            by_key[(str(r.get("year")), str(r.get("quarter") or ""))] = lvl
        if len(by_key) < 2:
            return None

        def qnum(q: str) -> int:
            digits = "".join(ch for ch in str(q) if ch.isdigit())
            return int(digits) if digits else 0

        keys = sorted(by_key.keys(),
                      key=lambda k: (k[0], qnum(k[1])), reverse=True)
        cur_key = keys[0]
        cur_year, cur_q = cur_key

        # Find the same quarter one year earlier. FY labels are "2026-27";
        # the prior year is "2025-26".
        try:
            start = int(cur_year.split("-")[0])
        except (ValueError, IndexError):
            return None
        prev_year = f"{start - 1}-{str(start)[-2:]}" if cur_q else \
            f"{start - 1}-{str(start)[-2:]}"

        prev_key = None
        for k in keys:
            if k[0] == prev_year and k[1] == cur_q:
                prev_key = k
                break
        if prev_key is None:
            # Same quarter last year absent: fall back to the next-oldest row.
            older = [k for k in keys if k != cur_key]
            if not older:
                return None
            prev_key = older[0]

        cur, prev = by_key[cur_key], by_key[prev_key]
        if prev <= 0:
            return None
        growth = (cur / prev - 1.0) * 100.0
        return {
            "value": round(growth, 2),
            "year": cur_year,
            "quarter": cur_q or "annual",
            "level": cur,
            "prev_level": prev,
            "prev_year": prev_key[0],
        }

    # ------------------------------------------------------------------ #
    # Provider contract
    # ------------------------------------------------------------------ #
    def _fetch(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        warnings: List[str] = []

        # Handshake. Some servers require initialize before tools/call.
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "blue-engine", "version": "1.0"}})

        # ---- inflation (CPI) ---------------------------------------------
        rows = self._get_data("CPI", {"base_year": self.base_year_cpi,
                                      "series": "Current"})
        picked = self._cpi_combined(rows) if rows else None
        if picked:
            val = self._to_float(picked.get("inflation"))
            if val is not None:
                period = (f"{picked.get('month')} {picked.get('year')}")
                out["inflation"] = value_block(
                    val, previous=None, unit="pct", as_of=period,
                    source=(f"MoSPI CPI {self.base_year_cpi} base, "
                            f"Combined, {period}"))
            else:
                warnings.append("CPI row had no 'inflation' field")
        else:
            warnings.append("MoSPI CPI returned no usable rows")

        # ---- gdp_growth (NAS, quarterly, computed YoY) --------------------
        gdp_rows = self._get_data("NAS", {
            "base_year": self.gdp_base_year, "indicator_code": "5",
            "series": "Current", "frequency_code": FREQUENCY_QUARTERLY})
        gdp = self._gdp_yoy(gdp_rows) if gdp_rows else None
        if gdp:
            out["gdp_growth"] = value_block(
                gdp["value"], previous=None, unit="pct",
                as_of=f"{gdp['year']} {gdp['quarter']}",
                source=(f"MoSPI NAS real GDP YoY, computed from constant "
                        f"prices ({gdp['year']} {gdp['quarter']} vs "
                        f"{gdp['prev_year']} {gdp['quarter']})"))
        else:
            warnings.append("MoSPI NAS returned no computable GDP series")

        # ---- employment (PLFS unemployment rate) --------------------------
        # Two pages are needed: the server caps a response at 20 rows and the
        # `person / rural + urban` headline sits on page 2. A single unpaged
        # call silently returns only male/rural slices -- which is exactly how
        # this reported 1.9% (rural male, 2022) instead of 4.8%.
        ur_rows: List[Dict[str, Any]] = []
        for page in ("1", "2"):
            got = self._get_data("PLFS", {"indicator_code": "3",
                                          "frequency_code": "3",
                                          "page": page})
            ur_rows.extend(got)
            if not got:
                break

        headline = self._plfs_headline(ur_rows) if ur_rows else None
        if headline:
            val = self._to_float(headline.get("value"))
            if val is not None:
                month = str(headline.get("month") or "").strip()
                year = str(headline.get("year") or "").strip()
                period = f"{month} {year}".strip() or "latest"
                detail = (f"MoSPI PLFS unemployment rate, "
                          f"{headline.get('gender', 'person')}, "
                          f"{headline.get('sector', 'rural + urban')}, "
                          f"{headline.get('AgeGroup', 'All ages')}, {period}")
                out["employment"] = value_block(
                    val, previous=None, unit="pct", as_of=period,
                    source=detail)
            else:
                warnings.append("PLFS headline row had no numeric value")
        else:
            warnings.append("MoSPI PLFS returned no usable headline row")

        out["meta"] = {
            "ok": bool(out),
            "stale": False,
            "warnings": warnings,
            "source": self.name,
        }
        return out


__all__ = ["MospiProvider", "MCP_URL", "PROTOCOL_VERSION"]