"""Read-only Mstock fund summary, for the MTF (margin trading facility) figures.

Flow stage: broker reads (dashboard only)
Status: PHASE 3 IMPLEMENTED (read-only; GET only; no order path)

Why direct and not through OpenAlgo: OpenAlgo's funds response has no MTF fields, while
Mstock's user/fundsummary does (MTF_UTILIZE = borrowed amount in use, MTF_COLLATERAL,
MTF_AVAILABLE_BALANCE). The owner approved this single extra read on 2026-11-20.

Configuration (environment only, never in the repo):
- MSTOCK_API_KEY       : the API key from the Mstock developer portal (valid about 1 year).
- MSTOCK_ACCESS_TOKEN  : the JWT access token. It expires within 12 hours, so it must be renewed
                         daily. Without it the dashboard falls back to the manual MTF field.

Open points, verified against tradingapi.mstock.com docs (2026-11-20):
- The docs show fundsummary as GET in the curl example and PUT in the other samples. GET is used
  here. If Mstock returns an error for GET, the state is "unavailable" with the message.
- X-PrivateKey is shown in the docs without a definition, so it is not sent.
"""
from __future__ import annotations

import math
import os
import time
from typing import Callable, Dict, Optional

import requests

BASE_URL = "https://api.mstock.trade"
FUNDSUMMARY_PATH = "/openapi/typeb/user/fundsummary"
TIMEOUT_S = 10.0
TTL_S = 60.0
ENV_KEY = "MSTOCK_API_KEY"
ENV_TOKEN = "MSTOCK_ACCESS_TOKEN"

_cache: Dict[str, object] = {"at": None, "value": None}


def _num(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "").strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def parse_fund_summary(body) -> Dict[str, Optional[float]]:
    """MTF figures from a fundsummary body. Takes the EQ row when several segments are returned."""
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("fundsummary has no data rows")
    rows = [r for r in rows if isinstance(r, dict)]
    row = next((r for r in rows if str(r.get("SEG", "")).upper() == "EQ"), rows[0])
    return {
        "mtf_utilize": _num(row.get("MTF_UTILIZE")),
        "mtf_collateral": _num(row.get("MTF_COLLATERAL")),
        "mtf_available_balance": _num(row.get("MTF_AVAILABLE_BALANCE")),
        "available_balance": _num(row.get("AVAILABLE_BALANCE")),
    }


def fetch_fund_summary(api_key: str, access_token: str,
                       get: Callable = requests.get) -> Dict:
    """One read-only call. Never raises: failures come back as state "unavailable" with the reason."""
    headers = {"X-Mirae-Version": "1", "Authorization": f"token {api_key}:{access_token}",
               "Content-Type": "application/json"}
    try:
        resp = get(BASE_URL + FUNDSUMMARY_PATH, headers=headers, timeout=TIMEOUT_S)
    except Exception as exc:  # network or TLS: the holdings page must still load
        return {"state": "unavailable", "error": f"request failed: {exc}", "mtf": None}
    try:
        body = resp.json()
    except ValueError:
        return {"state": "unavailable", "error": f"HTTP {resp.status_code}, not JSON", "mtf": None}
    if resp.status_code != 200 or str(body.get("status", "")).lower() == "error":
        return {"state": "unavailable",
                "error": str(body.get("message") or f"HTTP {resp.status_code}"), "mtf": None}
    try:
        return {"state": "ok", "error": None, "mtf": parse_fund_summary(body)}
    except ValueError as exc:
        return {"state": "unavailable", "error": str(exc), "mtf": None}


def read_mtf(get: Callable = requests.get, clock: Callable[[], float] = time.time,
             environ=None, ttl_s: float = TTL_S) -> Dict:
    """Cached MTF figures from Mstock. State is "not_configured" when the env vars are missing."""
    env = os.environ if environ is None else environ
    api_key, token = (env.get(ENV_KEY) or "").strip(), (env.get(ENV_TOKEN) or "").strip()
    if not api_key or not token:
        return {"state": "not_configured", "error": f"set {ENV_KEY} and {ENV_TOKEN}", "mtf": None}
    now = clock()
    cached_at, cached = _cache["at"], _cache["value"]
    if cached_at is not None and now - cached_at < ttl_s and cached is not None:
        return cached
    value = fetch_fund_summary(api_key, token, get=get)
    _cache["at"], _cache["value"] = now, value
    return value
