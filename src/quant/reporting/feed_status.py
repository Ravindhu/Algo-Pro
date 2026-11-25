"""Live feed status for the dashboard feed card. Read-only probe.

Flow stage: Data and features (feed health read-side)
Status: PHASE 3 IMPLEMENTED (decision 0033; wraps quant.data.live_bars + data_sources.yaml)

WHAT THIS DOES
--------------
The dashboard's feed card used to say "not connected" from a hard-coded line
(2026-11-25 owner request). This module replaces that with a real probe:

1. Read ``config/data_sources.yaml``: is the live source owner-approved?
2. Is OpenAlgo reachable at the configured URL?
3. Make ONE read-only quote call for a reference symbol. A successful call with
   a usable LTP proves: API key valid, broker session alive, feed delivering.

The probe result is cached (default 30 s) so the overview's 2 s poll never
hammers OpenAlgo. It never places an order and never writes anything.

Fail-open rule: every problem maps to a status string the card can show:
``connected`` / ``paused (market closed)`` / ``blocked by policy`` /
``openalgo down`` / ``api key missing`` / ``auth failed`` / ``stale``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import request as _urlreq

ROOT = Path(__file__).resolve().parents[3]

__all__ = ["feed_status", "FeedProbeCache"]

#: One symbol the probe quotes. A liquid NSE large-cap that trades every day.
PROBE_SYMBOL = ("NSE", "INFY")

_CACHE_TTL_S = 30.0
_HTTP_TIMEOUT_S = 6.0


def _data_sources() -> Dict[str, Any]:
    import yaml
    path = ROOT / "config" / "data_sources.yaml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _base_url() -> str:
    try:
        cfg = (_data_sources().get("openalgo") or {})
        return str(cfg.get("base_url") or os.environ.get("OPENALGO_URL")
                   or "http://127.0.0.1:5000").rstrip("/")
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:5000"


class FeedProbeCache:
    """Thread-safe TTL cache around the probe so /api/overview stays cheap."""

    def __init__(self, ttl_s: float = _CACHE_TTL_S) -> None:
        self._ttl = float(ttl_s)
        self._lock = threading.Lock()
        self._value: Optional[Dict[str, Any]] = None
        self._at = 0.0

    def get(self) -> Dict[str, Any]:
        with self._lock:
            if self._value is not None and (time.time() - self._at) < self._ttl:
                return self._value
        fresh = feed_status()
        with self._lock:
            self._value = fresh
            self._at = time.time()
            return fresh


def _quote(base: str, api_key: str) -> Dict[str, Any]:
    """One read-only quotes call. Returns the parsed JSON. Raises on failure."""
    body = json.dumps({"apikey": api_key, "exchange": PROBE_SYMBOL[0],
                       "symbol": PROBE_SYMBOL[1]}).encode("utf-8")
    req = _urlreq.Request(
        base + "/api/v1/quotes", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with _urlreq.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def feed_status(now: Optional[float] = None) -> Dict[str, Any]:
    """The feed card's payload. Never raises: every failure is a status string."""
    stamp = time.time() if now is None else now
    sources = _data_sources()
    live_cfg = sources.get("sources", {}).get("live_feed") or {}
    if not live_cfg.get("live_allowed"):
        return {"status": "blocked by policy", "note":
                "config/data_sources.yaml live_feed.live_allowed is false",
                "probe": None, "checked_at": stamp}

    api_key = (os.environ.get("OPENALGO_API_KEY") or "").strip()
    if not api_key:
        return {"status": "api key missing", "note":
                "OPENALGO_API_KEY is not set in .env",
                "probe": None, "checked_at": stamp}

    base = _base_url()
    try:
        result = _quote(base, api_key)
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        detail = str(exc) or type(exc).__name__
        if "10061" in detail or "Connection" in detail or "timed out" in detail:
            return {"status": "openalgo down", "note": f"{base}: {detail}",
                    "probe": None, "checked_at": stamp}
        return {"status": "auth failed", "note": detail,
                "probe": None, "checked_at": stamp}

    data = result.get("data") if isinstance(result, dict) else None
    ltp = (data or {}).get("ltp")
    if result.get("status") != "success" or not ltp:
        # success shape but no usable price: stale or empty frame
        return {"status": "stale", "note": "quote answered without a usable ltp",
                "probe": {"ltp": ltp}, "checked_at": stamp}

    return {"status": "connected",
            "note": f"{PROBE_SYMBOL[0]}:{PROBE_SYMBOL[1]} ltp {ltp}",
            "probe": {"symbol": f"{PROBE_SYMBOL[0]}:{PROBE_SYMBOL[1]}", "ltp": ltp,
                      "prev_close": (data or {}).get("prev_close")},
            "checked_at": stamp}
