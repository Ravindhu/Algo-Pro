"""
providers/base.py - the PortfolioProvider contract.

A provider is the ONLY place in the portfolio package allowed to touch the
network, the SDK, or the disk. Everything else (risk/optim/sizing/attribution)
is pure. This is what makes the engine offline-testable and upgrade-proof.

Degrade-never-halt contract
---------------------------
* ``fetch()`` NEVER raises. A failure is a partially-filled dict plus
  ``meta.warnings`` and ``meta.ok = False``.
* The canonical portfolio provider dict is documented in ``providers/__init__``.
* Stale data is allowed but flagged via ``meta.stale``.

Caching
-------
``CachedProvider`` adds a TTL cache with a filesystem spill (mirrors the
economics ``CachedProvider`` exactly) so a cold process after a network outage
still has the last good read. Portfolio inputs are intraday-to-daily; a TTL of a
few minutes is plenty and avoids hammering ``history`` per tick.
"""
from __future__ import annotations

import abc
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: Reserved keys that are never treated as sleeves.
_META_KEYS = {"meta"}


class PortfolioProvider(abc.ABC):
    """Abstract base for every portfolio data adapter."""

    name: str = "provider"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._log_fn = log

    def log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(f"[portfolio:{self.name}] {message}")
        else:
            logger.info("[portfolio:%s] %s", self.name, message)

    def fetch(self) -> Dict[str, Any]:
        """Return the canonical provider dict. NEVER raises."""
        started = time.time()
        try:
            payload = self._fetch()
            if not isinstance(payload, dict):
                raise TypeError(f"{self.name}._fetch() must return a dict, got "
                                f"{type(payload).__name__}")
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            self.log(f"fetch FAILED: {exc!r}")
            return {"meta": {"ok": False, "stale": False,
                             "warnings": [f"{self.name}: {exc!r}"],
                             "source": self.name}}
        payload.setdefault("meta", {})
        payload["meta"].setdefault("ok", True)
        payload["meta"].setdefault("stale", False)
        payload["meta"].setdefault("warnings", [])
        payload["meta"].setdefault("source", self.name)
        payload["meta"]["latency_ms"] = int((time.time() - started) * 1000)
        return payload

    @abc.abstractmethod
    def _fetch(self) -> Dict[str, Any]:
        """Provider-specific fetch. May raise; may do I/O."""
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "config_keys": sorted(self.config.keys())}


class CachedProvider(PortfolioProvider):
    """TTL + filesystem cache around another provider (decorator)."""

    def __init__(self, source: PortfolioProvider, *, ttl_seconds: float = 300.0,
                 cache_dir: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None):
        super().__init__(config=source.config, log=log)
        self.source = source
        self.name = f"cached:{source.name}"
        self._cache_key = source.name
        self.ttl = float(ttl_seconds)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._mem: Optional[Dict[str, Any]] = None
        self._mem_at: float = 0.0

    def _cache_file(self) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        safe = self._cache_key.replace(":", "_").replace("/", "_")
        return self.cache_dir / f"portfolio_{safe}.json"

    def _read_disk(self) -> Optional[Dict[str, Any]]:
        path = self._cache_file()
        if path is None or not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def _write_disk(self, payload: Dict[str, Any]) -> None:
        path = self._cache_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
        except Exception as exc:  # noqa: BLE001
            self.log(f"cache write failed: {exc!r}")

    def _fetch(self) -> Dict[str, Any]:
        now = time.time()
        if self._mem is not None and (now - self._mem_at) < self.ttl:
            payload = dict(self._mem)
            payload.setdefault("meta", {})
            payload["meta"]["cache"] = "memory"
            payload["meta"]["stale"] = False
            return payload
        fresh = self.source.fetch()
        fresh_ok = bool((fresh.get("meta") or {}).get("ok", False))
        if fresh_ok:
            self._mem, self._mem_at = fresh, now
            self._write_disk(fresh)
            out = dict(fresh)
            out.setdefault("meta", {})
            out["meta"]["cache"] = "miss"
            return out
        fallback = self._mem or self._read_disk()
        if fallback is not None:
            served = dict(fallback)
            served.setdefault("meta", {})
            served["meta"]["cache"] = "fallback"
            served["meta"]["stale"] = True
            served["meta"].setdefault("warnings", [])
            served["meta"]["warnings"] = list(served["meta"]["warnings"]) + [
                f"{self.source.name}: live fetch failed; serving cached copy"]
            self.log("live fetch failed; serving cached copy")
            return served
        return fresh


__all__ = ["PortfolioProvider", "CachedProvider"]