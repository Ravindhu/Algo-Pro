"""
base.py - the transport layer of the screener-india Python port.

WHAT THIS IS
------------
A faithful Python port of ``vendor/screener-india/src/client/base.ts`` plus the
four ``src/utils/*.ts`` helpers (cache, throttle, retry, parser). Same default
numbers, same login-wall detection, same retry predicate. The port exists so the
Blue engine never needs Node.js at runtime and so the screener becomes just
another provider the engines can call in-process.

WHY A PORT AND NOT A SUBPROCESS CALL TO THE NPM PACKAGE
-------------------------------------------------------
1. No Node.js runtime in the trading loop. A Node subprocess next to the POV
   slicer would be a new failure mode on the execution path; a Python module
   that returns a dict is not.
2. The repo is a **scraper of public HTML**. We want the parsing pinned and
   unit-testable in the same language (and the same test runner) as the engine.
3. It removes a whole dependency tree from a machine whose only job is trading.

CRITICAL: WHAT THIS MODULE IS *NOT* ALLOWED TO DO
-------------------------------------------------
* It never places an order. It never imports OpenAlgo.
* It never writes to the live config.
* It never raises into an engine's ``_run`` - network failures degrade to an
  ``ApiResponse`` with ``ok=False`` so a dead scraper cannot halt the pipeline.

UPSTREAM FIDELITY NOTES (deliberate deviations)
-----------------------------------------------
* Upstream's ``Throttle`` sleeps only *before* a request; identical here.
* Upstream's retry predicate never retries ``LOGIN_REQUIRED`` and never retries
  HTTP 4xx other than 429. Identical here - retrying a 403 would get the client
  rate-limited for no benefit.
* Upstream caches raw HTML keyed by the sha256 of the URL. We keep the sha256
  key (so cache semantics match exactly) but persist to disk as well, because a
  screener sweep runs once a day and a 5-minute in-memory TTL is worthless
  across processes. See ``cache_ttl_ms`` / ``cache_dir`` in the config.
* Upstream defaults ``minIntervalMs`` to 200 ms. We default to 400 ms and say
  so in the README: this is an unofficial scraper of a site we do not own, and
  being twice as polite costs a sweep a few extra seconds.

Data is public HTML from screener.in. Treat every value as *fundamental data of
unknown latency*, never as a live quote. For live prices, use the Market engine
/ OpenAlgo ``quotes()``. This distinction is enforced in ``ScreenerEngine``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants (mirrors vendor/screener-india/src/client/base.ts)
# --------------------------------------------------------------------------- #

#: Bumped whenever the port's parsing behaviour changes, so a stored result can
#: be traced to the exact parser that produced it. Mirrors upstream's
#: PARSER_VERSION, with a '.py' suffix to mark the port (not the TS package).
PARSER_VERSION = "1.0.0.py"

DEFAULT_BASE_URL = "https://www.screener.in"

#: A desktop Chrome UA. screener.in serves the same public HTML to any browser;
#: a default Python UA is more likely to be challenged by the CDN in front of it.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
)


# --------------------------------------------------------------------------- #
# Response envelope
# --------------------------------------------------------------------------- #
@dataclass
class Meta:
    """Provenance for one fetch. Mirrors upstream ``Meta``."""

    source_url: str
    fetched_at: str
    parser_version: str = PARSER_VERSION

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "parser_version": self.parser_version,
        }


@dataclass
class ApiResponse:
    """The envelope every client method returns. Mirrors upstream ``ApiResponse<T>``.

    ``ok`` is a port-only addition: upstream throws on transport failure, which
    is fine in TypeScript but useless inside an engine whose contract is
    "never raise" (see ``core/base.py``). A failed fetch is therefore a normal
    return value here, with ``ok=False`` and the reason in ``error``.
    """

    data: Any = None
    meta: Optional[Meta] = None
    warnings: list = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "meta": self.meta.as_dict() if self.meta else None,
            "warnings": list(self.warnings),
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# TTL cache (port of utils/cache.ts, with an optional disk tier)
# --------------------------------------------------------------------------- #
class TtlCache:
    """LRU-ish TTL cache with an optional on-disk tier.

    The memory tier ports upstream exactly. The disk tier is ours: a screener
    sweep is a daily batch job, and the single most important operational
    property of a public-site scraper is that a *re-run* does not re-hammer the
    site. Without the disk tier, every debugging loop would refetch 500 pages.
    """

    def __init__(self, ttl_ms: int = 300_000, cache_dir: Optional[str] = None,
                 max_entries: int = 512):
        self.ttl_ms = int(ttl_ms)
        self.max_entries = int(max_entries)
        self._store: Dict[str, tuple] = {}          # key -> (data, expires_at_ms)
        self._lock = threading.Lock()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- keys ------------------------------------------------------------ #
    @staticmethod
    def key_for(url: str) -> str:
        """sha256(url) - identical to upstream ``BaseClient.cacheKey``."""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    # -- memory tier ----------------------------------------------------- #
    def get(self, key: str) -> Optional[str]:
        now = time.time() * 1000.0
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                data, expires_at = entry
                if now <= expires_at:
                    return data
                self._store.pop(key, None)
        return self._disk_get(key)

    def set(self, key: str, data: str, ttl_ms: Optional[int] = None) -> None:
        ttl = self.ttl_ms if ttl_ms is None else int(ttl_ms)
        with self._lock:
            # Drop the oldest ~10% when full. A screener sweep touches a few
            # hundred URLs; unbounded growth would only matter over weeks.
            if len(self._store) >= self.max_entries:
                for k in list(self._store.keys())[: max(1, self.max_entries // 10)]:
                    self._store.pop(k, None)
            self._store[key] = (data, time.time() * 1000.0 + ttl)
        self._disk_set(key, data, ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    # -- disk tier ------------------------------------------------------- #
    def _disk_path(self, key: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{key}.html"

    def _disk_get(self, key: str) -> Optional[str]:
        p = self._disk_path(key)
        if p is None or not p.is_file():
            return None
        try:
            age_ms = (time.time() - p.stat().st_mtime) * 1000.0
            if age_ms > self.ttl_ms:
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _disk_set(self, key: str, data: str, ttl_ms: int) -> None:
        p = self._disk_path(key)
        if p is None:
            return
        try:
            # Write-then-rename so a crash mid-write cannot leave a truncated
            # page that would later be parsed as real data.
            tmp = p.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:  # pragma: no cover - disk full / perms
            logger.debug("screener cache write failed for %s: %s", key[:12], exc)


# --------------------------------------------------------------------------- #
# Throttle (port of utils/throttle.ts)
# --------------------------------------------------------------------------- #
class Throttle:
    """Per-scope minimum-gap limiter. Thread-safe (the engine may fan out)."""

    def __init__(self, default_interval_ms: int = 400):
        self.default_interval_ms = int(default_interval_ms)
        self._last_call_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, scope: str = "default", interval_ms: Optional[int] = None) -> None:
        interval = self.default_interval_ms if interval_ms is None else int(interval_ms)
        # Jitter: a perfectly periodic request train from one IP is the most
        # obvious bot signature there is. Upstream does not jitter; adding +/-20%
        # changes nothing about throughput and makes us a much duller target.
        if interval > 0:
            interval = int(interval * random.uniform(0.8, 1.2))
        with self._lock:
            last = self._last_call_at.get(scope, 0.0)
            now = time.time() * 1000.0
            elapsed = now - last
            sleep_ms = interval - elapsed
            # Reserve the slot *before* sleeping so concurrent callers queue
            # instead of all sleeping in parallel and then firing together.
            self._last_call_at[scope] = now + max(0.0, sleep_ms)
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)


# --------------------------------------------------------------------------- #
# Retry (port of utils/retry.ts)
# --------------------------------------------------------------------------- #
def should_retry(status: Optional[int], message: str) -> bool:
    """Port of upstream ``shouldRetry``.

    * ``LOGIN_REQUIRED`` -> never (more attempts cannot log us in).
    * no status (network error) -> yes.
    * 429 or any 5xx -> yes.
    * every other 4xx -> no (403/404 will not become 200 by waiting).
    """
    if message.startswith("LOGIN_REQUIRED"):
        return False
    if status is None:
        return True
    return status == 429 or status >= 500


def backoff_delay_s(attempt: int) -> float:
    """2**attempt seconds, as upstream. attempt=0 -> 1s, 1 -> 2s, 2 -> 4s."""
    return float(2 ** attempt)


__all__ = [
    "PARSER_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_USER_AGENT",
    "DEFAULT_ACCEPT",
    "Meta",
    "ApiResponse",
    "TtlCache",
    "Throttle",
    "should_retry",
    "backoff_delay_s",
]
