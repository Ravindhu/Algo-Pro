"""
providers/base.py - the MacroProvider contract.

A *provider* is the ONLY place in the economics package that is allowed to touch
the network, the SDK, or the disk. Everything else (features, models, scoring,
regime, the engine itself) is pure. This is what makes the economic engine
offline-testable and upgrade-proof: swap the provider, keep the maths.

Every provider must honour the *degrade-never-halt* contract:

    * ``fetch()`` NEVER raises. A failure is returned as a partially-filled dict
      plus a ``meta.warnings`` list and a ``meta.ok = False`` flag.
    * Values are returned in the canonical ``{"value": x, "previous": y,
      "unit": "...", "as_of": "YYYY-MM-DD", "source": "..."}`` block, or as a
      bare number for the simplest feeds.
    * Stale data is allowed but *flagged* via ``meta.stale`` so the engine can
      down-weight it.

Caching
-------
Macro prints are daily/weekly/monthly, so per-tick refetch is wasteful and
fragile. :class:`CachedProvider` adds a TTL cache with a filesystem fallback so
a cold start after a network outage still has *something* to score.

Design rules (mirrors core/base.py)
-----------------------------------
* No OpenAlgo internals imported here - only the public SDK, if any.
* Secrets are read from the environment first, then ``_restored/.env``; never
  hardcoded (see :mod:`blue_engine.economics.providers.secrets`).
"""
from __future__ import annotations

import abc
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def value_block(value: Any, previous: Any = None, unit: Optional[str] = None,
                as_of: Optional[str] = None,
                source: Optional[str] = None) -> Dict[str, Any]:
    """Build the canonical macro value block.

    Kept deliberately tiny and dependency-free so providers can import it
    without pulling in the whole package.
    """
    block: Dict[str, Any] = {"value": value}
    if previous is not None:
        block["previous"] = previous
    if unit is not None:
        block["unit"] = unit
    if as_of is not None:
        block["as_of"] = as_of
    if source is not None:
        block["source"] = source
    return block


class MacroProvider(abc.ABC):
    """Abstract base for all macro data adapters.

    Subclasses implement :meth:`_fetch` (may raise) and inherit the wrapping
    that turns any exception into a soft, warning-carrying result.
    """

    #: Human-readable provider name (appears in logs / reports).
    name: str = "provider"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._log_fn = log

    # ---- logging -------------------------------------------------------- #
    def log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(f"[{self.name}] {message}")
        else:
            logger.info("[%s] %s", self.name, message)

    # ---- public API ----------------------------------------------------- #
    def fetch(self) -> Dict[str, Any]:
        """Return a macro dict. NEVER raises.

        The returned mapping carries a reserved ``meta`` key::

            {"meta": {"ok": bool, "stale": bool, "warnings": [str],
                      "source": str, "as_of": iso8601}}
        """
        started = time.time()
        try:
            payload = self._fetch()
            if not isinstance(payload, dict):
                raise TypeError(
                    f"{self.name}._fetch() must return a dict, "
                    f"got {type(payload).__name__}")
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


class CachedProvider(MacroProvider):
    """TTL + filesystem cache decorator around another provider.

    Wraps a *source* provider and adds:

      * in-memory TTL cache (``ttl_seconds``),
      * a JSON spill to ``cache_dir`` so a cold process after a network outage
        still has the last good read,
      * a ``meta.stale`` flag when the served copy is beyond the TTL.

    The decorator pattern keeps caching orthogonal to the data source: any
    provider can be wrapped without knowing about the disk.
    """

    def __init__(self, source: MacroProvider, *, ttl_seconds: float = 900.0,
                 cache_dir: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None):
        super().__init__(config=source.config, log=log)
        self.source = source
        self.name = f"cached:{source.name}"
        # Stable cache identity captured at construction: the on-disk filename
        # must NOT depend on a swappable ``self.source`` (otherwise swapping the
        # source would look for a different file and lose the fallback).
        self._cache_key = source.name
        self.ttl = float(ttl_seconds)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._mem: Optional[Dict[str, Any]] = None
        self._mem_at: float = 0.0

    # ---- cache path ----------------------------------------------------- #
    def _cache_file(self) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        safe = self._cache_key.replace(":", "_").replace("/", "_")
        return self.cache_dir / f"macro_{safe}.json"

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

    # ---- fetch with caching -------------------------------------------- #
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
            self._mem = fresh
            self._mem_at = now
            self._write_disk(fresh)
            out = dict(fresh)
            out.setdefault("meta", {})
            out["meta"]["cache"] = "miss"
            return out

        # Source failed -> serve the freshest thing we have (memory, then disk).
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

        # Nothing cached -> propagate the soft failure untouched.
        return fresh


__all__ = ["MacroProvider", "CachedProvider", "value_block"]